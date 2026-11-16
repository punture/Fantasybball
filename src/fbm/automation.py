"""Scheduled FBM jobs: a job catalog in ET semantics, per-agent headless commands, and thin OS scheduler backends.

Design:
- Job times are defined in US/Eastern (NBA game days). OS schedulers (Task Scheduler, launchd, cron) fire in the
  computer's own time zone, so install converts them to that zone (not the owner's, which is for display only) for
  the *current* date. After a DST change in either zone re-run `fbm automation install`: the installer prints the
  next transition date, and `fbm health` warns when installed entries no longer match US/Eastern (install record).
- Every scheduler entry calls one entry point: `<python> -m fbm.cli automation run JOB` with the repo as working dir.
- Engine-only mode (free, no AI) can run on a denser schedule than the AI modes: lineup-guard checks every 15 minutes
  and only messages the owner when a lineup alert or injury update is due (`fbm alert ...`, NO_REPLY otherwise).
- Headless AI runs may run only `uv run fbm tool ...` shell commands plus read-only file access wherever the agent
  supports it. They may create proposals; approvals always come from the owner. Nothing auto-approves file writes.

Pure planning/rendering functions are unit-tested; functions that touch the OS are small and isolated.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from fbm.agents.render import TOOL_COMMAND
from fbm.config import PROJECT_ROOT
from fbm.settings import AGENTS, AUTOMATION_MODES, JOBS, Settings

ET = ZoneInfo("America/New_York")
LOG_DIR = PROJECT_ROOT / "reports" / "automation"
INSTALL_RECORD = LOG_DIR / "installed.json"  # job -> the zone and ET offset its scheduler entry was computed for
TASK_FOLDER = "\\FBM\\"
LAUNCHD_PREFIX = "com.fbm."
CRON_BEGIN = "# >>> FBM automation (managed by `fbm automation`; do not edit) >>>"
CRON_END = "# <<< FBM automation <<<"
LOCK_STALE_SECONDS = 3 * 3600
JOB_TIMEOUT_SECONDS = 2 * 3600
LOG_KEEP_DAYS = 30  # run logs older than this are deleted (engine-only lineup-guard alone writes 56 a day)

HEADLESS_AGENTS = ("copilot", "claude-code", "codex", "cursor", "gemini")


@dataclass(frozen=True)
class Trigger:
    """A wall-clock trigger. days: ISO weekdays (1=Mon..7=Sun); empty = every day."""

    hour: int
    minute: int = 0
    days: tuple[int, ...] = ()

    def describe(self) -> str:
        names = "Mon Tue Wed Thu Fri Sat Sun".split()
        when = ",".join(names[d - 1] for d in self.days) if self.days else "daily"
        return f"{when} {self.hour:02d}:{self.minute:02d}"


@dataclass(frozen=True)
class Job:
    name: str
    description: str
    et_triggers: tuple[Trigger, ...]
    skill: str | None  # skill run by AI modes
    role: str  # "routine" or "gm" (model role)
    engine_commands: tuple[tuple[str, ...], ...]  # deterministic `fbm` commands for engine-only mode
    engine_et_triggers: tuple[Trigger, ...] = ()  # engine-only schedule when it differs from et_triggers

    def triggers(self, mode: str | None = None) -> tuple[Trigger, ...]:
        """ET triggers for a mode: engine-only uses engine_et_triggers when set (AI runs keep et_triggers)."""
        return self.engine_et_triggers if mode == "engine-only" and self.engine_et_triggers else self.et_triggers


_GAME_WINDOW = tuple(Trigger(h, 0) for h in range(11, 23))  # 11:00-22:00 ET covers early weekend tips + nightly slate
# Engine-only alerts: every 15 minutes 10:00-23:45 ET (a lineup alert fires ~90 minutes before the first affected tip).
_ALERT_WINDOW = tuple(Trigger(h, m) for h in range(10, 24) for m in (0, 15, 30, 45))

CATALOG: dict[str, Job] = {
    j.name: j
    for j in (
        Job(
            "data-sync",
            "Sync NBA data after all games are final (~01:30 ET).",
            (Trigger(3, 0),),
            None,
            "routine",
            (("data", "sync"),),
        ),
        Job(
            "lineup-guard",
            "Lineup/IL check across the ET game window (incl. early weekend games): hourly with an AI agent; "
            "engine-only every 15 minutes, messaging you only when a lineup alert or injury update is due.",
            _GAME_WINDOW,
            "lineup-guard",
            "routine",
            (("alert", "lineup", "--send"), ("alert", "injuries", "--send")),
            _ALERT_WINDOW,
        ),
        Job(
            "daily-gm",
            "Daily GM run after games are final and data is synced (engine-only: the daily digest).",
            (Trigger(4, 0),),
            "daily-gm",
            "gm",
            (("snapshot",), ("digest", "--send")),
        ),
        Job(
            "weekly-retro",
            "Monday retro + preview after the fantasy week is final, before Monday games.",
            (Trigger(5, 0, (1,)),),
            "weekly-retro-preview",
            "gm",
            (("learn", "retro"), ("digest", "--kind", "weekly", "--send")),
        ),
        Job(
            "trade-scan",
            "Trade scan twice a week.",
            (Trigger(10, 0, (2, 5)),),
            "trade-scan",
            "gm",
            (),
        ),
        Job(
            "health",
            "Daily system health check (engine-only: messages you the FAIL checks, at most once a day).",
            (Trigger(3, 30),),
            "health",
            "routine",
            (("health", "--notify-on-fail"),),
        ),
    )
}
assert tuple(CATALOG) == JOBS


# ---------------------------------------------------------------- schedules


def trigger_runs(triggers: Sequence[Trigger], min_run: int = 3) -> list[tuple[Trigger, int, int]]:
    """(first, gap minutes, count) for each run of at least `min_run` evenly spaced triggers (same days, constant
    gap, in order, across midnight too); any other trigger is a run of one (gap 0)."""

    def gap(a: Trigger, b: Trigger) -> int:
        return (b.hour * 60 + b.minute - a.hour * 60 - a.minute) % 1440

    runs: list[tuple[Trigger, int, int]] = []
    i = 0
    while i < len(triggers):
        step = gap(triggers[i], triggers[i + 1]) if i + 1 < len(triggers) else 0
        j = i + 1
        while (
            j < len(triggers)
            and step
            and triggers[j].days == triggers[i].days
            and gap(triggers[j - 1], triggers[j]) == step
        ):
            j += 1
        count = j - i if j - i >= min_run else 1
        runs.append((triggers[i], step if count > 1 else 0, count))
        i += count
    return runs


def describe_triggers(triggers: Sequence[Trigger]) -> str:
    """'daily 10:00-23:45 every 15 min; Mon 05:00' (evenly spaced runs collapsed)."""
    parts = []
    for t, step, count in trigger_runs(triggers):
        if count == 1:
            parts.append(t.describe())
        else:
            end = (t.hour * 60 + t.minute + step * (count - 1)) % 1440
            parts.append(f"{t.describe()}-{end // 60:02d}:{end % 60:02d} every {step} min")
    return "; ".join(parts)


def _zone(tz: str | None) -> ZoneInfo | None:
    """An IANA zone, or None for this computer's local time (datetime.astimezone(None))."""
    return ZoneInfo(tz) if tz else None


def scheduler_timezone() -> str | None:
    """The zone OS schedulers fire in: the computer's system zone (cron, launchd and Task Scheduler ignore a shell's
    TZ). None stands for this process's local time, which is that zone whenever TZ is unset."""
    if not os.environ.get("TZ"):
        return None
    from fbm.setup_wizard import detect_timezone

    return detect_timezone(use_env=False)


def zone_label(tz: str | None) -> str:
    """A zone's name for messages: the IANA name, else this computer's zone name (e.g. "UTC", "CET")."""
    return tz or datetime.now().astimezone().tzname() or "local time"


def convert_trigger(t: Trigger, tz: str | None, on: date | None = None) -> Trigger:
    """Convert an ET trigger to wall time in `tz` (None: this computer's local time) using the UTC offsets in effect
    on `on` (default: today ET)."""
    ref = on or datetime.now(ET).date()
    et_dt = datetime(ref.year, ref.month, ref.day, t.hour, t.minute, tzinfo=ET)
    local = et_dt.astimezone(_zone(tz))
    shift = (local.date() - et_dt.date()).days
    days = tuple(sorted({(d - 1 + shift) % 7 + 1 for d in t.days}))
    return Trigger(local.hour, local.minute, days)


def owner_triggers(job: str, tz: str | None, on: date | None = None, mode: str | None = None) -> tuple[Trigger, ...]:
    """A job's ET triggers as wall times in `tz`: the owner's zone for display, the scheduler's zone to install."""
    seen: list[Trigger] = []
    for t in CATALOG[job].triggers(mode):
        c = convert_trigger(t, tz, on)
        if c not in seen:
            seen.append(c)
    return tuple(seen)


def et_offset_minutes(tz: str | None, on: date) -> int:
    """Minutes `tz` (None: this computer's local time) is ahead of US/Eastern at noon ET on `on`."""
    noon = datetime(on.year, on.month, on.day, 12, tzinfo=ET)
    local, et = noon.astimezone(_zone(tz)).utcoffset(), noon.utcoffset()
    assert local is not None and et is not None
    return int((local - et).total_seconds()) // 60


def next_offset_change(tz: str | None, start: date | None = None, horizon_days: int = 400) -> date | None:
    """First date after `start` on which the ET<->`tz` wall-clock offset changes (DST in either zone)."""
    start = start or datetime.now(ET).date()
    base = et_offset_minutes(tz, start)
    for i in range(1, horizon_days):
        d = start + timedelta(days=i)
        if et_offset_minutes(tz, d) != base:
            return d
    return None


# ---------------------------------------------------------------- validation


def validate(agent: str, mode: str, jobs: Sequence[str]) -> list[str]:
    """Return error messages for an invalid agent/mode/jobs combination (empty = OK)."""
    errors = []
    if agent not in AGENTS:
        errors.append(f"Unknown agent {agent!r}; choose one of {', '.join(AGENTS)}.")
    if mode not in AUTOMATION_MODES:
        errors.append(f"Unknown automation mode {mode!r}; choose one of {', '.join(AUTOMATION_MODES)}.")
    bad = [j for j in jobs if j not in CATALOG]
    if bad:
        errors.append(f"Unknown job(s) {', '.join(bad)}; choose from {', '.join(JOBS)}.")
    if mode == "scheduler" and agent not in HEADLESS_AGENTS:
        errors.append(
            f"Mode 'scheduler' runs your agent's CLI headless, but agent {agent!r} has no supported headless CLI. "
            "Use mode 'engine-only' (deterministic jobs, no AI) or 'agent-app' (schedule prompts in your agent app)."
        )
    return errors


# ---------------------------------------------------------------- agent commands


TOOL_CLI = (
    f"Call FBM only through its CLI: `{TOOL_COMMAND} <tool_name> key=value ...` (or pipe a JSON object of arguments "
    f"to `{TOOL_COMMAND} <tool_name> --stdin`); it prints the tool's JSON. `{TOOL_COMMAND} <tool_name> --doc` shows "
    "a tool's arguments. That is the only shell command allowed."
)


def job_prompt(job: str) -> str:
    skill = CATALOG[job].skill
    return (
        f"Run the {skill} skill (agents/skills/{skill}/SKILL.md) for my FBM league. This is an unattended "
        "scheduled run: use only fbm tool commands and read-only file access; do not edit files, change settings, "
        f"or run other shell commands. {TOOL_CLI} Numbers must come from fbm tool output. FBM never changes my "
        "league: produce insights, the lineup recommendation (lineup_steps, without confirm) and proposals "
        "(create_proposal) only. Never record approvals, never call proposal_steps, confirm_done or "
        "record_trade_response, never claim a "
        "move was made. Finish with a short report listing anything I need to do in Yahoo."
    )


def model_for(job: str, s: Settings) -> str:
    return s.routine_model if CATALOG[job].role == "routine" else s.gm_model


def agent_command(agent: str, prompt: str, model: str = "") -> list[str]:
    """Headless invocation for each agent CLI: shell limited to `uv run fbm tool` (or `uv`) + read-only where
    supported."""
    if agent == "copilot":
        # https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference
        # (shell filters match a command and its subcommands, so `uv` is the narrowest documented filter)
        cmd = ["copilot", "-p", prompt, "-s", "--no-ask-user", "--allow-tool=shell(uv:*)", "--deny-tool=write"]
    elif agent == "claude-code":
        # https://code.claude.com/docs/en/cli-reference (-p print mode; unlisted tools are not auto-approved)
        cmd = ["claude", "-p", prompt, f"--allowedTools=Bash({TOOL_COMMAND}:*),Read,Grep,Glob"]
        cmd += ["--disallowedTools=Edit,Write,NotebookEdit"]
    elif agent == "codex":
        # https://developers.openai.com/codex/noninteractive (fbm tool writes FBM's own database and journal, and
        # reads ESPN/Yahoo/Hashtag: workspace-write sandbox with network)
        cmd = ["codex", "exec", "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true"]
    elif agent == "cursor":
        # https://cursor.com/docs/cli/headless (without --force only allow-listed shell commands run;
        # `fbm agent install` allows Shell(uv) in .cursor/cli.json)
        cmd = ["cursor-agent", "-p", "--output-format", "text"]
    elif agent == "gemini":
        # https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/cli-reference.md
        cmd = ["gemini", "--approval-mode", "default", "--allowed-tools", f"run_shell_command({TOOL_COMMAND})"]
    else:
        raise ValueError(f"Agent {agent!r} has no headless CLI.")
    if model:
        cmd += ["--model", model]
    if agent in ("codex", "cursor"):
        cmd.append(prompt)
    elif agent == "gemini":
        cmd += ["-p", prompt]
    return cmd


def fbm_command(*args: str, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "fbm.cli", *args]


def plan_run(job: str, s: Settings, mode: str | None = None) -> list[list[str]]:
    """Commands `fbm automation run JOB` executes for the configured mode."""
    mode = mode or s.automation_mode
    j = CATALOG[job]
    if mode == "scheduler" and j.skill:
        return [agent_command(s.agent, job_prompt(job), model_for(job, s))]
    return [fbm_command(*c) for c in j.engine_commands]


# ---------------------------------------------------------------- run (entry point for every scheduler)

Runner = Callable[[list[str], Path], tuple[int, str]]


def _default_runner(cmd: list[str], cwd: Path) -> tuple[int, str]:
    exe = shutil.which(cmd[0]) or (cmd[0] if Path(cmd[0]).exists() else None)
    if exe is None:
        return 127, f"command not found: {cmd[0]}\n"
    try:
        p = subprocess.run(
            [exe, *cmd[1:]],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=JOB_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {e.timeout}s\n"
    return p.returncode, (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")


class LockBusy(RuntimeError):
    pass


def _acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - path.stat().st_mtime < LOCK_STALE_SECONDS:
            raise LockBusy(f"{path.name} held by another run") from None
        path.unlink(missing_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def _redact(cmd: list[str], prompt_len: int = 60) -> str:
    return " ".join((a if len(a) <= prompt_len else a[:prompt_len] + "...") for a in cmd)


def prune_logs(job: str, log_dir: Path = LOG_DIR, keep_days: int = LOG_KEEP_DAYS) -> int:
    """Delete this job's per-run logs (`<job>-<UTC stamp>.log`) older than `keep_days`. Returns how many."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for old in log_dir.glob(f"{job}-????????T??????Z.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
                removed += 1
        except OSError:
            continue  # in use or already gone: try again next run
    return removed


def run_job(
    job: str,
    s: Settings,
    mode: str | None = None,
    runner: Runner = _default_runner,
    log_dir: Path = LOG_DIR,
    now: datetime | None = None,
) -> tuple[int, Path | None]:
    """Run one job with a per-job lock; log to reports/automation/<job>-<UTC>.log. Returns (exit code, log path).

    Exit codes: 0 ok, 2 invalid job/config, 3 skipped (another run active), else the first failing command's code.
    Every command runs even after a failure: they are independent (a failed snapshot still sends the digest, built
    from the last snapshot with the failure in its Health section).
    """
    mode = mode or s.automation_mode
    if job not in CATALOG:
        return 2, None
    if mode == "scheduler" and validate(s.agent, mode, [job]):
        return 2, None
    now = now or datetime.now(UTC)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{job}-{now.strftime('%Y%m%dT%H%M%SZ')}.log"
    lock = log_dir / f"{job}.lock"
    try:
        _acquire_lock(lock)
    except LockBusy as e:
        log.write_text(f"skipped: {e}\n", encoding="utf-8")
        return 3, log
    code = 0
    try:
        prune_logs(job, log_dir)
        cmds = plan_run(job, s, mode)
        with log.open("w", encoding="utf-8") as f:
            f.write(f"job={job} mode={mode} agent={s.agent} start={now.isoformat()}\n")
            if not cmds:
                f.write("nothing to run for this job in this mode (AI-only job)\n")
            for cmd in cmds:
                f.write(f"$ {_redact(cmd)}\n")
                rc, out = runner(cmd, PROJECT_ROOT)
                f.write(out if out.endswith("\n") or not out else out + "\n")
                f.write(f"exit={rc}\n")
                if rc != 0 and code == 0:
                    code = rc
            f.write(f"end={datetime.now(UTC).isoformat()} result={code}\n")
    finally:
        lock.unlink(missing_ok=True)
    return code, log


# ---------------------------------------------------------------- backends: rendering (pure)


def entry_name(job: str) -> str:
    return f"fbm-{job}"


def task_xml(job: str, triggers: Sequence[Trigger], command: Sequence[str], workdir: Path, start: date) -> str:
    """Windows Task Scheduler XML: one CalendarTrigger per trigger, or per evenly spaced run of triggers with a
    Repetition (a task holds at most 48 triggers); local wall time."""
    day_tags = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    trig = []
    for t, step, count in trigger_runs(triggers):
        boundary = f"{start.isoformat()}T{t.hour:02d}:{t.minute:02d}:00"
        if t.days:
            days = "".join(f"<{day_tags[d - 1]} />" for d in t.days)
            sched = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        else:
            sched = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        repeat = ""
        if count > 1:  # `count` runs, `step` minutes apart; the duration ends just after the last one
            span = step * (count - 1) + 1
            repeat = (
                f"<Repetition><Interval>PT{step}M</Interval><Duration>PT{span // 60}H{span % 60}M</Duration>"
                "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
            )
        trig.append(f"    <CalendarTrigger>{repeat}<StartBoundary>{boundary}</StartBoundary>{sched}</CalendarTrigger>")
    args = subprocess.list2cmdline(list(command[1:]))
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-16"?>',
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
            f"  <RegistrationInfo><Description>FBM {escape(job)}: {escape(CATALOG[job].description)}"
            "</Description></RegistrationInfo>",
            "  <Triggers>",
            *trig,
            "  </Triggers>",
            "  <Principals><Principal><LogonType>InteractiveToken</LogonType>"
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>",
            "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>"
            "<ExecutionTimeLimit>PT2H</ExecutionTimeLimit><Enabled>true</Enabled></Settings>",
            '  <Actions Context="Author"><Exec>'
            f"<Command>{escape(command[0])}</Command><Arguments>{escape(args)}</Arguments>"
            f"<WorkingDirectory>{escape(str(workdir))}</WorkingDirectory></Exec></Actions>",
            "</Task>",
        ]
    )


def launchd_plist(job: str, triggers: Sequence[Trigger], command: Sequence[str], workdir: Path) -> str:
    def item(t: Trigger, d: int | None) -> str:
        wd = f"<key>Weekday</key><integer>{d % 7}</integer>" if d is not None else ""  # launchd: 0=Sunday
        return (
            f"<dict>{wd}<key>Hour</key><integer>{t.hour}</integer><key>Minute</key><integer>{t.minute}</integer></dict>"
        )

    intervals = [item(t, d) for t in triggers for d in (t.days or (None,))]
    args = "".join(f"<string>{escape(a)}</string>" for a in command)
    log = escape(str(LOG_DIR / f"{job}-launchd.log"))
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            '<plist version="1.0"><dict>',
            f"<key>Label</key><string>{LAUNCHD_PREFIX}{escape(job)}</string>",
            f"<key>ProgramArguments</key><array>{args}</array>",
            f"<key>WorkingDirectory</key><string>{escape(str(workdir))}</string>",
            f"<key>StartCalendarInterval</key><array>{''.join(intervals)}</array>",
            f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>",
            "</dict></plist>",
            "",
        ]
    )


def cron_lines(job: str, triggers: Sequence[Trigger], command: Sequence[str], workdir: Path) -> list[str]:
    import shlex

    cmd = f"cd {shlex.quote(str(workdir))} && {shlex.join(command)} >/dev/null 2>&1"
    hours: dict[tuple[tuple[int, ...], int], list[int]] = {}  # (days, hour) -> minutes, in trigger order
    for t in triggers:
        hours.setdefault((t.days, t.hour), []).append(t.minute)
    groups: dict[tuple[tuple[int, ...], str], list[int]] = {}  # (days, minutes) -> the hours sharing them
    for (days, hour), minutes in hours.items():
        groups.setdefault((days, ",".join(map(str, sorted(set(minutes))))), []).append(hour)
    lines = []
    for (days, minutes), hrs in groups.items():
        dow = ",".join(str(d % 7) for d in days) if days else "*"  # cron: 0=Sunday
        lines.append(f"{minutes} {','.join(map(str, hrs))} * * {dow} {cmd}  # {entry_name(job)}")
    return lines


def merge_crontab(existing: str, fbm_lines: Sequence[str]) -> str:
    """Replace (or remove, when fbm_lines is empty) the marked FBM block, preserving every other line."""
    out, inside = [], False
    for line in existing.splitlines():
        if line.strip() == CRON_BEGIN:
            inside = True
            continue
        if line.strip() == CRON_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if fbm_lines:
        if out:
            out.append("")
        out += [CRON_BEGIN, *fbm_lines, CRON_END]
    return "\n".join(out) + "\n" if out else ""


def crontab_block(existing: str) -> list[str]:
    lines = existing.splitlines()
    if CRON_BEGIN not in [x.strip() for x in lines]:
        return []
    start = [x.strip() for x in lines].index(CRON_BEGIN)
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == CRON_END), len(lines))
    return lines[start + 1 : end]


# ---------------------------------------------------------------- install plan


@dataclass(frozen=True)
class Entry:
    job: str
    triggers: tuple[Trigger, ...]
    command: tuple[str, ...]


def detect_backend() -> str:
    return {"Windows": "schtasks", "Darwin": "launchd"}.get(platform.system(), "cron")


def plan_entries(
    s: Settings, jobs: Sequence[str], mode: str, on: date | None = None, tz: str | None = None
) -> list[Entry]:
    """Scheduler entries: each job's ET triggers as wall times in the scheduler's zone `tz` (None: this computer's
    local time, which OS schedulers fire in; see scheduler_timezone). The owner's time zone is for display only."""
    entries = []
    for job in jobs:
        if mode == "engine-only" and not CATALOG[job].engine_commands:
            continue  # AI-only job (trade-scan)
        entries.append(Entry(job, owner_triggers(job, tz, on, mode), tuple(fbm_command("automation", "run", job))))
    return entries


def agent_app_instructions(s: Settings, jobs: Sequence[str], on: date | None = None) -> str:
    app = {
        "copilot": "the GitHub Copilot app (Automations view)",
        "codex": "the Codex / ChatGPT desktop app (Automations)",
        "claude-code": "Claude (scheduled tasks), if your plan/app offers them",
    }.get(s.agent, "your agent app's scheduled tasks/automations feature, if it has one")
    out = [
        f"Create one automation per job in {app}. Point it at this repository ({PROJECT_ROOT}),",
        f"use the times below (owner time zone {s.owner_timezone}; ET times in brackets), and paste the prompt.",
        "If your agent has a tool to create automations itself (e.g. the Copilot app's save_workflow tool), you can",
        "ask it to create these for you. Re-check times after DST changes.",
        "",
    ]
    for job in jobs:
        j = CATALOG[job]
        if not j.skill:
            out.append(
                f"- {job}: deterministic, no AI needed; use `fbm automation install --mode engine-only --jobs {job}`."
            )
            continue
        local = describe_triggers(owner_triggers(job, s.owner_timezone, on))
        et = describe_triggers(j.et_triggers)
        model = model_for(job, s) or "(agent default)"
        out += [f"- {job}  [{model}]  {local}  [ET: {et}]", f"  Prompt: {job_prompt(job)}", ""]
    return "\n".join(out)


def render_plan(entries: Sequence[Entry], backend: str, workdir: Path = PROJECT_ROOT, on: date | None = None) -> str:
    start = on or datetime.now(ET).date()
    parts = []
    for e in entries:
        if backend == "schtasks":
            parts.append(f"schtasks /Create /F /TN {TASK_FOLDER}{entry_name(e.job)} /XML <file>:")
            parts.append(task_xml(e.job, e.triggers, e.command, workdir, start))
        elif backend == "launchd":
            parts.append(f"{launchd_path(e.job)}:")
            parts.append(launchd_plist(e.job, e.triggers, e.command, workdir))
        else:
            parts += cron_lines(e.job, e.triggers, e.command, workdir)
    if backend == "cron" and entries:
        parts = ["crontab block:", CRON_BEGIN, *parts, CRON_END]
    return "\n".join(parts)


# ---------------------------------------------------------------- backends: OS side effects (thin)


def launchd_path(job: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_PREFIX}{job}.plist"


BACKEND_COMMANDS = {"cron": "crontab", "launchd": "launchctl", "schtasks": "schtasks"}
_MISSING_HINTS = {
    "crontab": "crontab not found: install cron, then run this again (Debian/Ubuntu: sudo apt install -y cron; "
    "Fedora: sudo dnf install -y cronie && sudo systemctl enable --now crond; Arch: sudo pacman -S cronie && "
    "sudo systemctl enable --now cronie)",
}


class SchedulerUnavailable(RuntimeError):
    """The OS scheduler's command isn't installed (e.g. no cron on a minimal Linux)."""


def missing_scheduler(backend: str, which: Callable[[str], str | None] = shutil.which) -> str:
    """Why this backend's scheduler command can't run ("" when it is on PATH)."""
    exe = BACKEND_COMMANDS[backend]
    return "" if which(exe) else _MISSING_HINTS.get(exe, f"{exe} not found on PATH")


def _run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, input=input_text, check=False)
    except FileNotFoundError:
        raise SchedulerUnavailable(_MISSING_HINTS.get(cmd[0], f"{cmd[0]} not found on PATH")) from None


def _read_crontab() -> str:
    p = _run(["crontab", "-l"])
    return p.stdout if p.returncode == 0 else ""


def install_entries(entries: Sequence[Entry], backend: str, workdir: Path = PROJECT_ROOT) -> list[str]:
    """Install the entries; one message per change ("FAILED ..." on failure). Raises SchedulerUnavailable when the
    scheduler's command is missing."""
    msgs, start = [], datetime.now(ET).date()
    if backend == "schtasks":
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for e in entries:
            xml = LOG_DIR / f"{entry_name(e.job)}.xml"
            xml.write_text(task_xml(e.job, e.triggers, e.command, workdir, start), encoding="utf-16")
            try:
                p = _run(["schtasks", "/Create", "/F", "/TN", f"{TASK_FOLDER}{entry_name(e.job)}", "/XML", str(xml)])
            finally:
                xml.unlink(missing_ok=True)
            msgs.append(f"{e.job}: {'installed' if p.returncode == 0 else 'FAILED ' + p.stderr.strip()}")
    elif backend == "launchd":
        for e in entries:
            path = launchd_path(e.job)
            path.parent.mkdir(parents=True, exist_ok=True)
            _run(["launchctl", "unload", str(path)])
            path.write_text(launchd_plist(e.job, e.triggers, e.command, workdir), encoding="utf-8")
            p = _run(["launchctl", "load", "-w", str(path)])
            msgs.append(f"{e.job}: {'installed' if p.returncode == 0 else 'FAILED ' + p.stderr.strip()}")
    else:
        lines = [x for e in entries for x in cron_lines(e.job, e.triggers, e.command, workdir)]
        current = _read_crontab()
        keep = [ln for ln in crontab_block(current) if not any(f"# {entry_name(e.job)}" in ln for e in entries)]
        p = _run(["crontab", "-"], merge_crontab(current, keep + lines))
        msgs.append(f"crontab: {'updated' if p.returncode == 0 else 'FAILED ' + p.stderr.strip()}")
    return msgs


def list_installed(backend: str) -> list[str]:
    if backend == "schtasks":
        p = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
        return sorted({ln.split(",")[0].strip('"') for ln in p.stdout.splitlines() if ln.startswith(f'"{TASK_FOLDER}')})
    if backend == "launchd":
        d = Path.home() / "Library" / "LaunchAgents"
        return sorted(str(x) for x in d.glob(f"{LAUNCHD_PREFIX}*.plist")) if d.exists() else []
    return crontab_block(_read_crontab())


def remove_installed(backend: str, jobs: Sequence[str] = JOBS) -> list[str]:
    msgs = []
    if backend == "schtasks":
        for name in list_installed(backend):
            job = name.removeprefix(TASK_FOLDER).removeprefix("fbm-")
            if job in jobs:
                p = _run(["schtasks", "/Delete", "/F", "/TN", name])
                msgs.append(f"{name}: {'removed' if p.returncode == 0 else 'FAILED ' + p.stderr.strip()}")
    elif backend == "launchd":
        for job in jobs:
            path = launchd_path(job)
            if path.exists():
                _run(["launchctl", "unload", "-w", str(path)])
                path.unlink()
                msgs.append(f"{path}: removed")
    else:
        current = _read_crontab()
        keep = [ln for ln in crontab_block(current) if not any(f"# {entry_name(j)}" in ln for j in jobs)]
        if crontab_block(current):
            p = _run(["crontab", "-"], merge_crontab(current, keep))
            msgs.append(f"crontab: {'updated' if p.returncode == 0 else 'FAILED ' + p.stderr.strip()}")
    return msgs


# ---------------------------------------------------------------- install record (read by `fbm health`)


def read_install_record(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """job -> {zone, offset_min, installed_on}: what each installed entry's wall times were computed for."""
    try:
        data = json.loads((path or INSTALL_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_install_record(record: dict[str, dict[str, Any]], path: Path) -> None:
    if not record:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def save_install_record(
    entries: Sequence[Entry], tz: str | None, on: date | None = None, path: Path | None = None
) -> None:
    """After a successful install: remember the zone and ET offset each entry's wall times were computed for."""
    today = on or datetime.now(ET).date()
    path = path or INSTALL_RECORD
    record = read_install_record(path)
    for e in entries:
        record[e.job] = {
            "zone": zone_label(tz),
            "offset_min": et_offset_minutes(tz, today),
            "installed_on": today.isoformat(),
        }
    _write_install_record(record, path)


def forget_install_record(jobs: Sequence[str], path: Path | None = None) -> None:
    path = path or INSTALL_RECORD
    _write_install_record({k: v for k, v in read_install_record(path).items() if k not in jobs}, path)
