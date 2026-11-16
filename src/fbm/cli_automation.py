"""`fbm automation` commands (scheduled jobs). Registered by importing this module from fbm.cli."""

from __future__ import annotations

from datetime import datetime

import typer

from fbm import automation as auto
from fbm.cli import app, console
from fbm.settings import load_settings

automation_app = typer.Typer(no_args_is_help=True, help="Scheduled jobs (OS scheduler / agent app automations).")
app.add_typer(automation_app, name="automation")


def _jobs(jobs: str | None, default: tuple[str, ...]) -> list[str]:
    if jobs is None:
        return list(default)
    return [j.strip() for j in jobs.split(",") if j.strip()]


@automation_app.command("jobs")
def jobs_cmd() -> None:
    """Show the job catalog (ET schedule, skill, model role, engine-only commands)."""
    s = load_settings()
    for j in auto.CATALOG.values():
        et = auto.describe_triggers(j.et_triggers)
        local = auto.describe_triggers(auto.owner_triggers(j.name, s.owner_timezone))
        engine = " && ".join("fbm " + " ".join(c) for c in j.engine_commands) or "(AI only)"
        console.print(f"[bold]{j.name}[/] — {j.description}")
        console.print(f"  ET: {et}\n  {s.owner_timezone}: {local}")
        if j.engine_et_triggers:
            console.print(f"  engine-only ET: {auto.describe_triggers(j.engine_et_triggers)}")
        console.print(f"  skill: {j.skill or '-'}  role: {j.role}  engine-only: {engine}")


@automation_app.command("run")
def run_cmd(
    job: str = typer.Argument(..., help="Job name, e.g. lineup-guard."),
    mode: str = typer.Option(None, "--mode", help="Override [automation] mode (scheduler | engine-only)."),
) -> None:
    """Run one job now (the entry point every scheduler calls). Logs to reports/automation/."""
    s = load_settings()
    errors = auto.validate(s.agent, mode or s.automation_mode, [job])
    if errors:
        for e in errors:
            console.print(f"[red]{e}[/]")
        raise typer.Exit(2)
    code, log = auto.run_job(job, s, mode)
    console.print(f"{job}: exit {code}" + (f" — log {log}" if log else ""))
    raise typer.Exit(code)


@automation_app.command("install")
def install_cmd(
    mode: str = typer.Option(None, "--mode", help="none | agent-app | scheduler | engine-only (default: settings)."),
    jobs: str = typer.Option(
        None, "--jobs", help="Comma-separated jobs (default: settings [automation] jobs, else every engine-only job)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print exactly what would be installed; change nothing."),
) -> None:
    """Install scheduled jobs for the chosen mode."""
    s = load_settings()
    mode = mode or s.automation_mode
    selected = _jobs(jobs, s.automation_jobs)
    if mode == "engine-only" and jobs is None and not selected:  # no jobs in settings: every engine-capable job
        selected = [name for name, job in auto.CATALOG.items() if job.engine_commands]
    errors = auto.validate(s.agent, mode, selected)
    if errors:
        for e in errors:
            console.print(f"[red]{e}[/]")
        raise typer.Exit(2)
    if mode == "none" or not selected:
        console.print("Automation mode 'none' or no jobs selected: nothing to install.")
        return
    if mode == "agent-app":
        console.print(auto.agent_app_instructions(s, selected), markup=False, highlight=False)
        return
    tz = auto.scheduler_timezone()  # the OS scheduler fires on this computer's clock, whatever [owner] timezone says
    entries = auto.plan_entries(s, selected, mode, tz=tz)
    skipped = sorted(set(selected) - {e.job for e in entries})
    if skipped:
        console.print(f"[yellow]Skipped (AI-only, nothing to run in {mode}): {', '.join(skipped)}[/]")
    backend = auto.detect_backend()
    today = datetime.now(auto.ET).date()
    note = (
        f"Times are this computer's clock ({auto.zone_label(tz)}): the US/Eastern job times converted for today's "
        "offset."
    )
    if change := auto.next_offset_change(tz):
        note += f" The offset changes on {change}: re-run `fbm automation install` then (`fbm health` warns)."
    if auto.et_offset_minutes(tz, today) != auto.et_offset_minutes(s.owner_timezone, today):
        note += (
            f" Your time zone ({s.owner_timezone}) differs from this computer's, so these times differ from yours; "
            "`fbm automation jobs` shows them in yours."
        )
    if dry_run:
        console.print(f"[bold]Dry run[/] — backend {backend}, mode {mode}.")
        console.print(note, markup=False)
        if problem := auto.missing_scheduler(backend):
            console.print(problem, style="yellow", markup=False)
        console.print(auto.render_plan(entries, backend), markup=False, highlight=False)
        return
    try:
        msgs = auto.install_entries(entries, backend)
    except auto.SchedulerUnavailable as err:
        console.print(str(err), style="red", markup=False)
        raise typer.Exit(1) from None
    for m in msgs:
        console.print(m, markup=False)
    if any("FAILED" in m for m in msgs):
        raise typer.Exit(1)
    auto.save_install_record(entries, tz, today)
    console.print(note, markup=False)


@automation_app.command("list")
def list_cmd() -> None:
    """List installed FBM scheduler entries."""
    try:
        items = auto.list_installed(auto.detect_backend())
    except auto.SchedulerUnavailable as err:
        console.print(f"No FBM scheduler entries installed ({err}).", markup=False)
        return
    for i in items:
        console.print(i, markup=False, highlight=False)
    if not items:
        console.print("No FBM scheduler entries installed.")


@automation_app.command("remove")
def remove_cmd(
    jobs: str = typer.Option(None, "--jobs", help="Comma-separated jobs (default: all FBM entries)."),
) -> None:
    """Remove FBM scheduler entries (never touches other entries)."""
    selected = _jobs(jobs, auto.JOBS)
    try:
        msgs = auto.remove_installed(auto.detect_backend(), selected)
    except auto.SchedulerUnavailable as err:
        console.print(f"No FBM scheduler entries to remove ({err}).", markup=False)
        return
    for m in msgs:
        console.print(m, markup=False)
    if not msgs:
        console.print("No FBM scheduler entries to remove.")
    if any("FAILED" in m for m in msgs):
        raise typer.Exit(1)
    auto.forget_install_record(selected)
    mode = load_settings().automation_mode
    if jobs is None and mode in ("engine-only", "scheduler"):
        console.print(
            f"[automation] mode is still {mode}, so `fbm setup` would install these jobs again. To stop that: "
            "`fbm setup --automation none --skip-data --yes`.",
            markup=False,
        )
