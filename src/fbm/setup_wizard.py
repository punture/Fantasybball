"""`fbm setup` logic: settings file merging, timezone handling, and the idempotent setup steps.

Pure where possible; every external command goes through an injectable `runner` so tests never touch the network,
Yahoo, or the user's real config. Secrets are never handled here: Yahoo, Flaim, Telegram and Gmail credentials are
entered by the user in the `fbm auth` / `fbm flaim login` / `fbm notify setup` prompts and stored by fbm.credentials
(the OS credential store, or FBM's private secrets file on machines without one).

Free mode (Yahoo league + Hashtag off, FBM's own ESPN engine) is a fully supported setup; Hashtag Basketball Premium
is an optional paid upgrade.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
import zoneinfo
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fbm.config import DATA_DIR, MEMORY_DIR, PROJECT_ROOT
from fbm.settings import AGENTS, AUTOMATION_MODES, EXAMPLE_PATH, JOBS, SETTINGS_PATH

OK, SKIP, FAIL, PENDING = "OK", "SKIP", "FAIL", "PENDING"

# CLI each agent runs headless / is launched with (used for the prerequisite check only).
AGENT_CLIS: dict[str, tuple[str, ...]] = {
    "copilot": ("copilot",),
    "claude-code": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor-agent", "cursor"),
    "gemini": ("gemini",),
    "generic": (),
}

YAHOO_GUIDE = (
    "Apply for Yahoo Fantasy API access at https://sports.yahoo.com/developer/access/ (approval can take weeks).",
    "After the 'access enabled' email, create a NEW Yahoo app (API permissions: Fantasy Sports Read; "
    "redirect URI https://localhost:8080) at https://developer.yahoo.com/apps/create/.",
    "Submit that app's Client ID at https://sports.yahoo.com/developer/application-confirmation/.",
    "Run `fbm auth` and enter the Client ID/Secret in its prompts (stored in your OS credential store).",
    "Run `fbm league select`, then `fbm settings`.",
    "Run `fbm setup --league-source yahoo --yes`, then `fbm snapshot` and `fbm ids`.",
)

# ---- free mode: Yahoo connection routes ([yahoo] route) -------------------------------------------------------
YAHOO_ROUTES = ("auto", "flaim", "session", "api")
ROUTE_LABELS = {  # explicit routes, as shown in the setup report ("auto" shows none)
    "flaim": "Flaim",
    "session": "your Yahoo web session",
    "api": "the official Yahoo API",
}
# Flaim: a free hosted read-only connector with its own approved Yahoo app (docs/flaim.md).
FLAIM_GUIDE = (
    "Connect Yahoo through Flaim (free, read-only; Flaim has its own approved Yahoo access): sign up at "
    "https://flaim.app and connect Yahoo at https://flaim.app/leagues, then run `fbm flaim login` and "
    "`fbm flaim check` (see docs/flaim.md).",
    "Pick your league and switch FBM to it: `fbm league select`, `fbm setup --league-source yahoo --yes`, then "
    "`fbm snapshot` and `fbm ids`.",
)
SESSION_ROUTE_GUIDE = (
    "Connect with your own Yahoo sign-in: `fbm yahoo login` (opens a browser window once; read-only), then "
    "`fbm yahoo check`. Yahoo doesn't document this route and its Terms of Service restrict automated access: a "
    "grey area, used at your own discretion (see docs/yahoo.md).",
)
API_LONG_TERM = (
    "Long term, also apply for official Yahoo API access (https://sports.yahoo.com/developer/access/, 3-8 weeks; "
    "see docs/yahoo.md) and switch with `fbm setup --yahoo-route api --yes` once approved.",
)
AUTO_ROUTE_GUIDE = (
    "Recommended (free, read-only): connect through Flaim. Run `fbm setup --yahoo-route flaim --yes`, then follow "
    "docs/flaim.md (sign up at https://flaim.app, connect Yahoo, `fbm flaim login`).",
    "Or connect now with your own Yahoo sign-in: `fbm yahoo login` (read-only; undocumented by Yahoo and a Terms "
    "of Service grey area, see docs/yahoo.md), pick your league and let it switch FBM to it, then `fbm yahoo check`.",
)
TELEGRAM_GUIDE = (
    "Telegram alerts: create a bot with @BotFather in Telegram, then run `fbm notify setup` (it stores the bot token "
    "and your chat id; prompted, never shown) and `fbm notify test --channel telegram` (see docs/telegram.md).",
)
TELEGRAM_OFFER = (
    "Optional: lineup and injury alerts plus a daily digest on Telegram: `fbm setup --telegram --yes`, then "
    "`fbm notify setup` (see docs/telegram.md).",
)
RUN_24_7_GUIDE = (
    "Run it 24/7 on this computer (optional): either OpenClaw, which also lets you chat with your GM on Telegram "
    "(`openclaw/install.sh`, or `openclaw/install.ps1` on Windows; see docs/openclaw.md), or without OpenClaw "
    "`fbm automation install --mode engine-only` (deterministic jobs on the OS scheduler, no AI).",
)
# ---- end free mode -----------------------------------------------------------------------------------------------

QUIET_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
LEAGUE_ID_RE = re.compile(r"^\d+$")

# Hashtag Basketball Premium is an optional paid upgrade: FBM runs fully on its free ESPN engine without it.
FREE_ENGINE = "Free ESPN engine (Hashtag off)"
HASHTAG_GUIDE = (
    "Optional paid upgrade: Hashtag Basketball Premium (https://hashtagbasketball.com/premium/, Patreon) can become "
    "FBM's data layer instead of the free ESPN engine. Import your Yahoo league in Hashtag's Premium area, then run "
    "`fbm setup --hashtag --yes`. Not needed: FBM is fully supported without it.",
)
HASHTAG_CONNECT_GUIDE = (
    "Connect Hashtag: `fbm hashtag login` (sign in once in the browser window it opens), then `fbm hashtag check` "
    "and `fbm hashtag import`. FBM makes no suggestions until Hashtag is healthy.",
)


@dataclass
class StepResult:
    step: str
    status: str
    detail: str = ""


@dataclass
class SetupOptions:
    """Values the user chose. None = keep the existing value (or the example default on first run)."""

    agent: str | None = None
    gm_model: str | None = None
    reviewer_model: str | None = None
    routine_model: str | None = None
    timezone: str | None = None
    automation: str | None = None
    jobs: list[str] | None = None
    league_source: str | None = None
    email: bool | None = None
    quiet_hours: str | None = None
    expiry_hours: float | None = None
    skip_data: bool = False
    yahoo_approved: bool = False
    yahoo_web_session: bool = False  # interactive setup: connect now via `fbm yahoo login`
    hashtag: bool | None = None  # [hashtag] enabled (optional paid data layer; real league only)
    hashtag_league_id: str | None = None  # [hashtag] league_id (default: the Yahoo league id)
    hashtag_login: bool = False  # interactive setup: run `fbm hashtag login` + check + import after setup
    yahoo_route: str | None = None  # [yahoo] route: auto | flaim | session | api
    telegram: bool | None = None  # add/remove "telegram" in [notify] channels
    telegram_chat_id: str | None = None  # [notify] telegram_chat_id (for `fbm notify setup`)
    flaim_login: bool = False  # interactive setup: run `fbm flaim login` (+ league select) after setup
    notify_setup: bool = False  # interactive setup: run `fbm notify setup` after setup

    def updates(self) -> dict[tuple[str, str], Any]:
        mapping = {
            ("agent", "name"): self.agent,
            ("agent", "gm_model"): self.gm_model,
            ("agent", "reviewer_model"): self.reviewer_model,
            ("agent", "routine_model"): self.routine_model,
            ("owner", "timezone"): self.timezone,
            ("automation", "mode"): self.automation,
            ("automation", "jobs"): self.jobs,
            ("league", "source"): self.league_source,
            ("notify", "email_enabled"): self.email,
            ("notify", "quiet_hours"): self.quiet_hours,
            ("proposals", "expiry_hours"): self.expiry_hours,
            ("hashtag", "enabled"): self.hashtag,
            ("hashtag", "league_id"): self.hashtag_league_id,
            ("yahoo", "route"): self.yahoo_route,
            ("notify", "telegram_chat_id"): self.telegram_chat_id,
        }
        return {k: v for k, v in mapping.items() if v is not None}


def effective_channels(notify: dict[str, Any]) -> list[str]:
    """Enabled notification channels as FBM reads [notify]: `channels`, else ["email"] when email_enabled."""
    channels = notify.get("channels")
    if isinstance(channels, list):
        return [str(c) for c in channels]
    return ["email"] if notify.get("email_enabled") else []


def notify_channels(notify: dict[str, Any], telegram: bool | None, email: bool | None) -> list[str] | None:
    """[notify] channels after --telegram/--email (None = leave the file alone). Starts from the effective channels;
    existing order is kept and telegram goes first."""
    if telegram is None and not (isinstance(notify.get("channels"), list) and email is not None):
        return None
    channels = effective_channels(notify)
    if telegram and "telegram" not in channels:
        channels.insert(0, "telegram")
    if email and "email" not in channels:
        channels.append("email")
    drop = {name for name, on in (("telegram", telegram), ("email", email)) if on is False}
    return [c for c in channels if c not in drop]


# ---------------------------------------------------------------- validation


def valid_timezone(name: str) -> bool:
    if not name or name.startswith(("/", ".")) or ".." in name:
        return False
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def detect_timezone(use_env: bool = True) -> str:
    """Best-effort local IANA zone; "UTC" when it can't be determined. use_env=False ignores TZ and reads the
    system zone (the one OS schedulers fire in)."""
    env = os.environ.get("TZ", "") if use_env else ""
    if valid_timezone(env):
        return env
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell:
            script = (
                "$x=$null; [void][System.TimeZoneInfo]::TryConvertWindowsIdToIanaId("
                "([System.TimeZoneInfo]::Local).Id, [ref]$x); $x"
            )
            try:
                out = subprocess.run(
                    [shell, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=15
                ).stdout.strip()
                if valid_timezone(out):
                    return out
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            target = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
                if valid_timezone(name):
                    return name
        except OSError:
            pass
        tz_file = Path("/etc/timezone")
        if tz_file.exists():
            name = tz_file.read_text(encoding="utf-8").strip()
            if valid_timezone(name):
                return name
    return "UTC"


def validate(opts: SetupOptions) -> list[str]:
    errors: list[str] = []
    if opts.agent is not None and opts.agent not in AGENTS:
        errors.append(f"--agent must be one of {', '.join(AGENTS)}")
    if opts.timezone is not None and not valid_timezone(opts.timezone):
        errors.append(f"--timezone '{opts.timezone}' is not a valid IANA time zone (e.g. America/New_York)")
    if opts.automation is not None and opts.automation not in AUTOMATION_MODES:
        errors.append(f"--automation must be one of {', '.join(AUTOMATION_MODES)}")
    if opts.jobs is not None:
        bad = [j for j in opts.jobs if j not in JOBS]
        if bad:
            errors.append(f"unknown --jobs {', '.join(bad)} (choose from {', '.join(JOBS)})")
    if opts.league_source is not None and opts.league_source not in ("demo", "yahoo"):
        errors.append("--league-source must be demo or yahoo")
    if opts.quiet_hours and not QUIET_HOURS_RE.match(opts.quiet_hours):
        errors.append("--quiet-hours must look like 23:00-07:00 (or be empty)")
    if opts.expiry_hours is not None and not 1 <= opts.expiry_hours <= 168:
        errors.append("--expiry-hours must be between 1 and 168")
    if opts.hashtag_league_id and not LEAGUE_ID_RE.match(opts.hashtag_league_id):
        errors.append("--hashtag-league-id must be the numeric league id (e.g. 30273)")
    if opts.hashtag and opts.league_source == "demo":
        errors.append("--hashtag needs your real Yahoo league (the demo league runs on FBM's ESPN engine)")
    if opts.yahoo_route is not None and opts.yahoo_route not in YAHOO_ROUTES:
        errors.append(f"--yahoo-route must be one of {', '.join(YAHOO_ROUTES)}")
    return errors


def parse_jobs(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [j.strip() for j in text.split(",") if j.strip() and j.strip().lower() != "none"]


# ---------------------------------------------------------------- settings file


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


_SECTION_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(#.*)?$")
_KEY_RE = re.compile(r'^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)("(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^\s#]+)(.*)$')


def render_settings(example_text: str, values: dict[str, dict[str, Any]]) -> str:
    """Fill the example (keeping its comments) with `values`; keys/sections not in the example are appended."""
    remaining = {s: dict(kv) for s, kv in values.items()}
    out: list[str] = []
    section = ""
    for line in example_text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            _flush(out, section, remaining)
            section = m.group(1)
            out.append(line)
            continue
        k = _KEY_RE.match(line)
        if k and section in remaining and k.group(2) in remaining[section]:
            value = remaining[section].pop(k.group(2))
            line = f"{k.group(1)}{k.group(2)}{k.group(3)}{toml_value(value)}{k.group(5)}"
        out.append(line)
    _flush(out, section, remaining)
    for sec, kv in remaining.items():
        if kv:
            out.extend(["", f"[{sec}]", *(f"{key} = {toml_value(v)}" for key, v in kv.items())])
    return "\n".join(out).rstrip("\n") + "\n"


def _flush(out: list[str], section: str, remaining: dict[str, dict[str, Any]]) -> None:
    """Insert keys from `remaining[section]` that the example doesn't have, before the section's trailing blanks."""
    kv = remaining.get(section, {})
    if not section or not kv:
        return
    blanks = 0
    while out and out[-1].strip() == "":
        out.pop()
        blanks += 1
    out.extend(f"{key} = {toml_value(v)}" for key, v in kv.items())
    out.extend([""] * blanks)
    remaining[section] = {}


def merged_settings(existing_text: str | None, opts: SetupOptions) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if existing_text:
        for sec, kv in tomllib.loads(existing_text).items():
            if isinstance(kv, dict):
                values[sec] = dict(kv)
    channels = notify_channels(values.get("notify", {}), opts.telegram, opts.email)
    for (sec, key), value in opts.updates().items():
        values.setdefault(sec, {})[key] = value
    if channels is not None:
        values.setdefault("notify", {})["channels"] = channels
    automation = values.get("automation", {})
    if automation.get("mode") == "engine-only" and not automation.get("jobs"):
        automation["jobs"] = engine_only_jobs()  # engine-only with no jobs would install nothing
    return values


def engine_only_jobs() -> list[str]:
    """Default engine-only jobs: every catalog job with deterministic `fbm` commands (AI-only jobs are skipped)."""
    from fbm.automation import CATALOG

    return [name for name, job in CATALOG.items() if job.engine_commands]


def write_settings(
    opts: SetupOptions, path: Path = SETTINGS_PATH, example: Path = EXAMPLE_PATH
) -> tuple[StepResult, dict[str, dict[str, Any]]]:
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        values = merged_settings(existing, opts)
        text = render_settings(example.read_text(encoding="utf-8"), values)
        tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return StepResult("Settings", FAIL, f"{path.name}: {exc}"), {}
    if existing == text:
        return StepResult("Settings", SKIP, f"{_rel(path)} unchanged"), values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return StepResult("Settings", OK, f"{'updated' if existing else 'created'} {_rel(path)}"), values


def current_values(path: Path = SETTINGS_PATH, example: Path = EXAMPLE_PATH) -> dict[str, dict[str, Any]]:
    """Effective settings (existing file, else the example defaults)."""
    for p in (path, example):
        if p.exists():
            try:
                return tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError:
                continue
    return {}


def _rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- steps


Runner = Callable[[Sequence[str]], tuple[int, str]]


def fbm_command(*args: str) -> list[str]:
    # Not `-m fbm.cli`: running cli.py as __main__ skips the command groups registered on the imported `fbm.cli`.
    return [sys.executable, "-c", "from fbm.cli import app; app()", *args]


def default_runner(cmd: Sequence[str]) -> tuple[int, str]:
    """Run a command (output captured). Returns (returncode, combined output)."""
    try:
        proc = subprocess.run(
            list(cmd), cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _missing_command(output: str) -> bool:
    return "No such command" in output


def _tail(output: str, n: int = 300) -> str:
    text = " ".join(output.split())
    return text[-n:]


def check_prerequisites(agent: str, which: Callable[[str], str | None] = shutil.which) -> list[StepResult]:
    uv = which("uv") or (os.environ.get("UV") if os.environ.get("UV") and Path(os.environ["UV"]).exists() else None)
    results = [
        StepResult(
            "Python >= 3.12",
            OK if sys.version_info >= (3, 12) else FAIL,
            sys.version.split()[0],
        ),
        StepResult("uv", OK if uv else FAIL, uv or "not on PATH: https://docs.astral.sh/uv/"),
        StepResult("git", OK if which("git") else FAIL, which("git") or "not on PATH: https://git-scm.com/"),
    ]
    clis = AGENT_CLIS.get(agent, ())
    if clis:
        found = next((which(c) for c in clis if which(c)), None)
        results.append(
            StepResult(
                f"{agent} CLI",
                OK if found else SKIP,
                found or f"`{clis[0]}` not on PATH (only needed for terminal/headless use)",
            )
        )
    return results


def setup_memory(memory_dir: Path = MEMORY_DIR, template_dir: Path | None = None) -> StepResult:
    from fbm.gm import memory

    created = memory.ensure_memory(memory_dir, template_dir or memory.TEMPLATE_DIR)
    if created:
        return StepResult("Memory", OK, f"created {', '.join(created)}")
    return StepResult("Memory", SKIP, "memory/ already present")


def prior_season_loaded(db_path: Path = DATA_DIR / "fbm.db") -> bool:
    if not db_path.exists():
        return False
    try:
        from datetime import date

        from fbm.data.espn import current_season
        from fbm.data.queries import store_status
        from fbm.store import db as store

        prior = current_season(date.today()) - 1
        seasons = store_status(store.connect())["seasons"]
        return any(int(s["season"]) == prior and (s["boxed"] or 0) > 0 for s in seasons)
    except Exception:  # noqa: BLE001 — any store problem just means "run the backfill"
        return False


def run_data(runner: Runner, skip: bool, backfilled: bool) -> list[StepResult]:
    if skip:
        return [StepResult("NBA data", SKIP, "--skip-data (run `fbm data backfill` and `fbm data sync` later)")]
    results = []
    if backfilled:
        results.append(StepResult("NBA backfill", SKIP, "prior season already loaded"))
    else:
        code, out = runner(fbm_command("data", "backfill"))
        results.append(
            StepResult("NBA backfill", OK if code == 0 else FAIL, "prior season" if code == 0 else _tail(out))
        )
    code, out = runner(fbm_command("data", "sync"))
    results.append(StepResult("NBA sync", OK if code == 0 else FAIL, "up to date" if code == 0 else _tail(out)))
    return results


def run_demo(
    runner: Runner, source: str, demo_file: Path = DATA_DIR / "demo_league.json", skip_data: bool = False
) -> StepResult:
    if source != "demo":
        return StepResult("Demo league", SKIP, "league source is yahoo")
    if demo_file.exists():
        return StepResult("Demo league", SKIP, "demo league already built")
    if skip_data:
        return StepResult("Demo league", SKIP, "needs NBA data: run `fbm data backfill`, then `fbm demo build`")
    code, out = runner(fbm_command("demo", "build"))
    return StepResult("Demo league", OK if code == 0 else FAIL, "built" if code == 0 else _tail(out))


def run_optional(runner: Runner, name: str, args: Sequence[str], ok_detail: str) -> StepResult:
    """Run a sibling command group that may not exist in older checkouts."""
    code, out = runner(fbm_command(*args))
    if code == 0:
        return StepResult(name, OK, ok_detail)
    if _missing_command(out):
        return StepResult(name, SKIP, f"`fbm {' '.join(args[:2])}` not available in this version")
    return StepResult(name, FAIL, _tail(out))


def run_agent_install(runner: Runner, agent: str) -> StepResult:
    return run_optional(runner, "Agent files", ["agent", "install", "--agent", agent], f"installed for {agent}")


def run_automation_install(runner: Runner, mode: str, jobs: Sequence[str], dry_run: bool = False) -> StepResult:
    if mode == "none":
        return StepResult("Automation", SKIP, "mode none (run the GM on demand from your agent)")
    args = ["automation", "install", "--mode", mode]
    if jobs:
        args += ["--jobs", ",".join(jobs)]
    if dry_run:
        args.append("--dry-run")
    return run_optional(runner, "Automation", args, f"{mode}: {', '.join(jobs) or 'default jobs'}")


_ROUTE_PENDING = {
    "flaim": "using the demo league until you connect Yahoo through Flaim (`fbm flaim login`)",
    "session": "using the demo league until you sign in with `fbm yahoo login`",
    "api": "using the demo league until Yahoo API access is approved (`fbm auth`)",
    "auto": "using the demo league until Yahoo API access is approved or `fbm yahoo login`",
}


def yahoo_status(source: str, approved: bool, league_toml: Path, route: str = "auto") -> list[StepResult]:
    via = f" via {ROUTE_LABELS[route]}" if route in ROUTE_LABELS else ""
    if source == "yahoo" and league_toml.exists():
        return [StepResult("Yahoo league", OK, f"connected{via} (config/league.toml)")]
    if source == "yahoo":
        return [StepResult("Yahoo league", PENDING, "source is yahoo but no league selected: run `fbm league select`")]
    if approved:
        return [StepResult("Yahoo league", PENDING, "access approved: follow the Yahoo steps below")]
    return [StepResult("Yahoo league", PENDING, _ROUTE_PENDING.get(route, _ROUTE_PENDING["auto"]))]


def secret_store_status() -> StepResult:
    """Where secrets live: the OS keyring, or FBM's file store on machines without one (fbm.credentials)."""
    from fbm import credentials

    if credentials.backend() == credentials.KEYRING and not credentials.keyring_usable():
        return StepResult(
            "Secret store",
            FAIL,
            f"{credentials.BACKEND_ENV}=keyring but no OS keyring is usable: unlock/install one or unset the variable",
        )
    return StepResult("Secret store", OK, credentials.describe())


def run_health(runner: Runner) -> StepResult:
    code, out = runner(fbm_command("health"))
    if code == 0:
        return StepResult("Health", OK, "no FAIL checks (see `fbm health` for WARNs)")
    return StepResult("Health", FAIL, "some checks FAIL: run `fbm health` for fixes")


def default_hashtag_league_id(league_toml: Path) -> str:
    """The Yahoo league id from config/league.toml (`<game>.l.<id>`), which is also Hashtag's league id."""
    try:
        data = tomllib.loads(league_toml.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    key = str(data.get("league", {}).get("league_key", ""))
    return key.split(".l.")[-1] if ".l." in key else ""


def _hashtag_health() -> Any:
    from fbm.hashtag import health
    from fbm.store import db

    return health.status(db.connect(), enabled=True)


def hashtag_status(
    values: dict[str, dict[str, Any]], league_toml: Path, health: Callable[[], Any] | None = None
) -> StepResult:
    """Data layer: FBM's free ESPN engine (Hashtag off; OK for a real league) or the optional Hashtag Basketball
    Premium upgrade (checked for health). SKIP for the demo league."""
    source = values.get("league", {}).get("source", "demo")
    ht = values.get("hashtag", {})
    enabled = bool(ht.get("enabled"))
    if source != "yahoo":
        if enabled:
            return StepResult("Hashtag", FAIL, "enabled but the league is the demo: set `[hashtag] enabled = false`")
        return StepResult("Hashtag", SKIP, "demo league runs on FBM's free ESPN engine")
    if not enabled:
        return StepResult("Hashtag", OK, FREE_ENGINE)
    league_id = str(ht.get("league_id") or "")
    if not league_id:
        guess = default_hashtag_league_id(league_toml)
        return StepResult(
            "Hashtag", FAIL, f"enabled without league_id: `fbm setup --hashtag-league-id {guess or 'ID'}`"
        )
    try:
        st = (health or _hashtag_health)()
    except Exception as err:  # noqa: BLE001 - report, don't crash setup
        return StepResult("Hashtag", PENDING, f"league {league_id}: health unknown ({type(err).__name__})")
    if st.healthy:
        return StepResult("Hashtag", OK, f"league {league_id}: healthy (suggestions on)")
    first = st.reasons[0] if st.reasons else "not healthy"
    return StepResult("Hashtag", PENDING, f"league {league_id}: {first} — suggestions paused until healthy")


@dataclass
class SetupReport:
    results: list[StepResult] = field(default_factory=list)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return any(r.status == FAIL for r in self.results)

    def _yahoo_steps(self) -> list[str]:
        route = str(self.settings.get("yahoo", {}).get("route") or "auto")
        approved = any(r.step == "Yahoo league" and r.detail.startswith("access approved") for r in self.results)
        if route == "api" or approved:
            return list(YAHOO_GUIDE)
        if route == "flaim":
            return [*FLAIM_GUIDE, *API_LONG_TERM]
        if route == "session":
            return [*SESSION_ROUTE_GUIDE, *API_LONG_TERM]
        return [*AUTO_ROUTE_GUIDE, *API_LONG_TERM]

    def next_steps(self) -> list[str]:
        steps: list[str] = []
        source = self.settings.get("league", {}).get("source", "demo")
        notify = self.settings.get("notify", {})
        if any(r.status == FAIL for r in self.results):
            steps.append("Fix the FAIL steps above, then re-run `fbm setup --check`.")
        connected = source == "yahoo" and not any(
            r.step == "Yahoo league" and r.status == PENDING for r in self.results
        )
        if not connected:
            steps.extend(self._yahoo_steps())
        hashtag = next((r for r in self.results if r.step == "Hashtag"), None)
        if hashtag is not None and hashtag.status == PENDING and self.settings.get("hashtag", {}).get("enabled"):
            steps.extend(HASHTAG_CONNECT_GUIDE)
        channels = effective_channels(notify)
        if "telegram" in channels:
            steps.extend(TELEGRAM_GUIDE)
        if notify.get("email_enabled"):
            steps.append("Run `fbm notify setup` to store the Gmail app password (prompted, never shown).")
        if not channels:
            steps.extend(TELEGRAM_OFFER)
        if self.settings.get("automation", {}).get("mode", "none") == "none":
            steps.extend(RUN_24_7_GUIDE)
        if connected and not self.settings.get("hashtag", {}).get("enabled"):
            steps.extend(HASHTAG_GUIDE)
        steps.append(
            "Open your AI agent in this folder (restart it so it picks up the FBM agent files) and ask: "
            '"Open the GM office".'
        )
        return steps


def check(
    runner: Runner = default_runner,
    settings_path: Path = SETTINGS_PATH,
    memory_dir: Path = MEMORY_DIR,
    league_toml: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    hashtag_health: Callable[[], Any] | None = None,
) -> SetupReport:
    """Verification only: no files written, no installs."""
    from fbm.config import LEAGUE_CONFIG_PATH

    values = current_values(settings_path)
    agent = values.get("agent", {}).get("name", "generic")
    report = SetupReport(settings=values)
    report.results.extend(check_prerequisites(agent, which))
    report.results.append(secret_store_status())
    report.results.append(
        StepResult("Settings", OK, _rel(settings_path))
        if settings_path.exists()
        else StepResult("Settings", FAIL, "config/settings.toml missing: run `fbm setup`")
    )
    report.results.append(
        StepResult("Memory", OK, "present")
        if (memory_dir / "playbook.md").exists()
        else StepResult("Memory", FAIL, "memory/ missing: run `fbm setup`")
    )
    report.results.append(run_optional(runner, "Agent files", ["agent", "status"], "see `fbm agent status`"))
    source = values.get("league", {}).get("source", "demo")
    route = str(values.get("yahoo", {}).get("route") or "auto")
    report.results.extend(yahoo_status(source, False, league_toml or LEAGUE_CONFIG_PATH, route))
    report.results.append(hashtag_status(values, league_toml or LEAGUE_CONFIG_PATH, hashtag_health))
    report.results.append(run_health(runner))
    return report


def run_setup(
    opts: SetupOptions,
    runner: Runner = default_runner,
    settings_path: Path = SETTINGS_PATH,
    example_path: Path = EXAMPLE_PATH,
    memory_dir: Path = MEMORY_DIR,
    template_dir: Path | None = None,
    league_toml: Path | None = None,
    demo_file: Path = DATA_DIR / "demo_league.json",
    backfilled: Callable[[], bool] = prior_season_loaded,
    which: Callable[[str], str | None] = shutil.which,
    on_step: Callable[[StepResult], None] | None = None,
    hashtag_health: Callable[[], Any] | None = None,
) -> SetupReport:
    """Run every setup step in order. Each step is idempotent and reported OK/SKIP/FAIL/PENDING."""
    from fbm.config import LEAGUE_CONFIG_PATH

    report = SetupReport()
    league_toml = league_toml or LEAGUE_CONFIG_PATH
    if opts.hashtag and not opts.hashtag_league_id:
        existing = current_values(settings_path, example_path).get("hashtag", {}).get("league_id")
        if not existing:
            opts.hashtag_league_id = default_hashtag_league_id(league_toml) or None

    def add(*results: StepResult) -> None:
        for r in results:
            report.results.append(r)
            if on_step:
                on_step(r)

    settings_result, values = write_settings(opts, settings_path, example_path)
    report.settings = values or current_values(settings_path, example_path)
    agent = report.settings.get("agent", {}).get("name", "generic")
    add(*check_prerequisites(agent, which))
    add(secret_store_status())
    add(settings_result)
    add(setup_memory(memory_dir, template_dir))
    add(*run_data(runner, opts.skip_data, False if opts.skip_data else backfilled()))
    source = report.settings.get("league", {}).get("source", "demo")
    add(run_demo(runner, source, demo_file, opts.skip_data))
    add(run_agent_install(runner, agent))
    automation = report.settings.get("automation", {})
    add(run_automation_install(runner, automation.get("mode", "none"), automation.get("jobs", [])))
    route = str(report.settings.get("yahoo", {}).get("route") or "auto")
    add(*yahoo_status(source, opts.yahoo_approved, league_toml, route))
    add(hashtag_status(report.settings, league_toml, hashtag_health))
    add(run_health(runner))
    return report
