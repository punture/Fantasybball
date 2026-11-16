"""`fbm setup` (guided install), `fbm backup`, and the legacy `fbm sync-memory`. Registered from fbm.cli."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
from rich.table import Table

from fbm import setup_wizard as wiz
from fbm.cli import app, console
from fbm.settings import AGENTS, AUTOMATION_MODES, JOBS

_COLORS = {wiz.OK: "green", wiz.SKIP: "dim", wiz.FAIL: "red", wiz.PENDING: "yellow"}

_AGENT_HELP = {
    "copilot": "GitHub Copilot (app, CLI, or VS Code)",
    "claude-code": "Claude Code",
    "codex": "OpenAI Codex CLI",
    "cursor": "Cursor",
    "gemini": "Gemini CLI",
    "generic": "any other agent that can run shell commands",
}
_AUTOMATION_HELP = {
    "none": "no schedule: you open your agent and ask the GM when you want",
    "agent-app": "your agent app's own scheduled automations (if it has them)",
    "scheduler": "the OS scheduler runs your agent headless (needs its CLI)",
    "engine-only": "the OS scheduler runs deterministic fbm jobs only, no AI",
}
_ROUTE_HELP = {
    "flaim": "recommended: Flaim, a free hosted read-only connector with its own approved Yahoo access "
    "(sign in at flaim.app, then `fbm flaim login`)",
    "session": "your own Yahoo browser sign-in cookies (`fbm yahoo login`); undocumented by Yahoo, a Terms of "
    "Service grey area",
    "api": "your own approved Yahoo developer app (`fbm auth`); Yahoo approval takes 3-8 weeks",
    "auto": "FBM's default: a saved Yahoo web session if any, else the official API",
}


def _choice(question: str, options: dict[str, str], default: str) -> str:
    console.print(f"\n[bold]{question}[/]")
    for key, text in options.items():
        console.print(f"  [cyan]{key}[/] — {text}")
    while True:
        answer = typer.prompt("Choice", default=default).strip()
        if answer in options:
            return answer
        console.print(f"[red]Pick one of: {', '.join(options)}[/]")


def _interactive(opts: wiz.SetupOptions, current: dict) -> None:
    """Ask, one question at a time, for every option not given as a flag."""
    agent_s, owner_s = current.get("agent", {}), current.get("owner", {})
    console.print("[bold]FBM setup[/] — press Enter to keep the value in brackets.")
    if opts.agent is None:
        opts.agent = _choice("Which AI agent will run your GM?", _AGENT_HELP, agent_s.get("name", "generic"))
    if opts.gm_model is None:
        console.print("\nModels use your agent's own names; leave empty for the agent's default.")
        opts.gm_model = typer.prompt("GM model (decisions; a frontier model)", default=agent_s.get("gm_model", ""))
    if opts.reviewer_model is None:
        opts.reviewer_model = typer.prompt(
            "Reviewer model (ideally a different model family than the GM)",
            default=agent_s.get("reviewer_model", ""),
        )
    if opts.routine_model is None:
        opts.routine_model = typer.prompt(
            "Routine model (cheap: lineup guard, health)", default=agent_s.get("routine_model", "")
        )
    if opts.timezone is None:
        tz_default = owner_s.get("timezone") if owner_s.get("timezone", "UTC") != "UTC" else wiz.detect_timezone()
        while True:
            tz = typer.prompt("\nYour time zone (IANA, e.g. America/New_York)", default=tz_default).strip()
            if wiz.valid_timezone(tz):
                opts.timezone = tz
                break
            console.print("[red]Not a valid IANA time zone.[/]")
    if opts.automation is None:
        opts.automation = _choice(
            "How should the GM run on a schedule?",
            _AUTOMATION_HELP,
            current.get("automation", {}).get("mode", "none"),
        )
    if opts.jobs is None and opts.automation != "none":
        existing = ",".join(current.get("automation", {}).get("jobs", [])) or "data-sync,lineup-guard,daily-gm"
        while True:
            jobs = wiz.parse_jobs(typer.prompt(f"Jobs ({', '.join(JOBS)})", default=existing)) or []
            if all(j in JOBS for j in jobs):
                opts.jobs = jobs
                break
            console.print("[red]Unknown job name.[/]")
    route = str(current.get("yahoo", {}).get("route") or "auto")
    on_yahoo = current.get("league", {}).get("source", "demo") == "yahoo"
    if opts.yahoo_route is None:
        console.print(
            "\nFBM reads your Yahoo league read-only (it never changes it). Until you connect, it runs a demo league "
            "over real NBA players."
        )
        # Recommend Flaim to new installs; a connected league keeps its route on Enter (switching needs a sign-in).
        default = route if route != "auto" or on_yahoo else "flaim"
        opts.yahoo_route = _choice("How should FBM read your Yahoo league?", _ROUTE_HELP, default)
    if opts.league_source is None:
        opts.league_source = current.get("league", {}).get("source", "demo")
        if opts.league_source != "yahoo" or opts.yahoo_route != route:
            _ask_connect(opts)  # still on the demo, or the connected league moves to a new route
    notify = current.get("notify", {})
    if opts.telegram is None:
        channels = notify.get("channels")
        opts.telegram = typer.confirm(
            "\nLineup/injury alerts and a daily digest on Telegram (free; you create a bot)?",
            default="telegram" in channels if isinstance(channels, list) else True,
        )
    if opts.email is None:
        opts.email = typer.confirm("Email notifications via Gmail?", default=bool(notify.get("email_enabled")))
    if opts.telegram or opts.email:
        opts.notify_setup = typer.confirm(
            "Run `fbm notify setup` after setup (type your bot token / app password into its prompts)?", default=True
        )
    if opts.quiet_hours is None and opts.email:
        opts.quiet_hours = typer.prompt(
            "Quiet hours, local time (e.g. 23:00-07:00; empty = none)", default=notify.get("quiet_hours", "")
        ).strip()
    if opts.expiry_hours is None:
        opts.expiry_hours = typer.prompt(
            "Hours before an unapproved proposal expires",
            default=float(current.get("proposals", {}).get("expiry_hours", 18)),
            type=float,
        )


def _ask_connect(opts: wiz.SetupOptions) -> None:
    """Still on the demo league: ask whether to connect Yahoo on the chosen route (run after settings are written)."""
    route = opts.yahoo_route or "auto"
    if route == "flaim":
        console.print(
            "First, for free: sign up at https://flaim.app and connect your Yahoo account at https://flaim.app/leagues."
        )
        opts.flaim_login = typer.confirm(
            "Done? Connect FBM to Flaim after setup (`fbm flaim login`, opens a browser window once)", default=True
        )
    elif route == "session":
        opts.yahoo_web_session = typer.confirm(
            "Sign in to Yahoo after setup (`fbm yahoo login`, opens a browser window once)?", default=True
        )
    else:
        opts.yahoo_approved = typer.confirm("Has Yahoo approved your API access?", default=False)
        if not opts.yahoo_approved and route == "auto":
            opts.yahoo_web_session = typer.confirm(
                "Connect now with your Yahoo sign-in instead (opens a browser window once)?", default=True
            )


def _fbm(*args: str) -> bool:
    """Run an fbm command in the foreground (its own prompts); True on success."""
    return subprocess.run(wiz.fbm_command(*args), check=False).returncode == 0


def _flaim_interactive() -> None:
    """Route flaim: sign in to Flaim, pick the league if none is selected, then offer to leave the demo league."""
    from fbm.config import LEAGUE_CONFIG_PATH

    if not _fbm("flaim", "login"):
        console.print("[red]`fbm flaim login` failed; fix it (see docs/flaim.md) and run it again.[/]")
        return
    if not LEAGUE_CONFIG_PATH.exists() and not _fbm("league", "select"):
        console.print("[red]`fbm league select` failed; run it again once Flaim lists your league.[/]")
        return
    if wiz.current_values().get("league", {}).get("source") == "yahoo":
        return
    if not typer.confirm("Switch FBM from the demo league to your Yahoo league now?", default=True):
        console.print("When ready: `fbm setup --league-source yahoo --yes`, then `fbm snapshot` and `fbm ids`.")
        return
    wiz.write_settings(wiz.SetupOptions(league_source="yahoo"))
    for args in (["settings"], ["snapshot"], ["ids"]):
        _fbm(*args)


def _yahoo_interactive() -> None:
    """Walk an approved user through connecting Yahoo; credentials are typed into `fbm auth`'s own prompts."""
    for line in wiz.YAHOO_GUIDE[1:3]:
        console.print(f"  • {line}")
    if not typer.confirm("Done both? Run `fbm auth` now", default=True):
        return
    for args in (["auth"], ["league", "select"], ["settings"]):
        if subprocess.run(wiz.fbm_command(*args), check=False).returncode != 0:
            console.print(f"[red]`fbm {' '.join(args)}` failed; fix it and re-run `fbm setup`.[/]")
            return
    wiz.write_settings(wiz.SetupOptions(league_source="yahoo"))
    for args in (["snapshot"], ["ids"]):
        subprocess.run(wiz.fbm_command(*args), check=False)


def _hashtag_interactive(opts: wiz.SetupOptions, current: dict) -> None:
    """Real (Yahoo) league only: Hashtag Basketball Premium is an optional paid upgrade over the free ESPN engine.
    Ask, write [hashtag], then offer sign-in."""
    from fbm.config import LEAGUE_CONFIG_PATH

    ht = current.get("hashtag", {})
    if opts.hashtag is None:
        console.print(
            "\nFBM runs your league on its [bold]free ESPN engine[/]. Optional paid upgrade: [bold]Hashtag Basketball "
            "Premium[/] (https://hashtagbasketball.com/premium/, Patreon) as the data layer, with your Yahoo league "
            "imported in Hashtag's Premium area. With it on, FBM pauses suggestions whenever Hashtag can't be read."
        )
        opts.hashtag = typer.confirm(
            "Use Hashtag Premium (you have it, with this league imported)?", default=bool(ht.get("enabled", False))
        )
    if not opts.hashtag:
        wiz.write_settings(wiz.SetupOptions(hashtag=False))
        console.print(f"[green]{wiz.FREE_ENGINE}[/]: fully supported. Add Hashtag later with `fbm setup --hashtag`.")
        return
    if opts.hashtag_league_id is None:
        default = str(ht.get("league_id") or wiz.default_hashtag_league_id(LEAGUE_CONFIG_PATH))
        while True:
            league_id = typer.prompt("Hashtag league id (your Yahoo league id)", default=default).strip()
            if wiz.LEAGUE_ID_RE.match(league_id):
                opts.hashtag_league_id = league_id
                break
            console.print("[red]Numbers only, e.g. 30273.[/]")
    wiz.write_settings(wiz.SetupOptions(hashtag=True, hashtag_league_id=opts.hashtag_league_id))
    status = wiz.hashtag_status(wiz.current_values(), LEAGUE_CONFIG_PATH)
    if status.status == wiz.OK:
        console.print(f"[green]Hashtag: {status.detail}[/]")
        return
    opts.hashtag_login = typer.confirm("Sign in to Hashtag now (opens a browser window once)?", default=True)
    if not opts.hashtag_login:
        return
    for args in (["hashtag", "login"], ["hashtag", "check"], ["hashtag", "import"]):
        if subprocess.run(wiz.fbm_command(*args), check=False).returncode != 0:
            console.print(f"[red]`fbm {' '.join(args)}` failed; fix it, then run it again.[/]")
            return


def _print_report(report: wiz.SetupReport) -> None:
    table = Table(title="FBM setup report")
    for col in ("Step", "Status", "Detail"):
        table.add_column(col)
    for r in report.results:
        table.add_row(r.step, f"[{_COLORS[r.status]}]{r.status}[/]", r.detail)
    console.print(table)
    s = report.settings
    agent, auto = s.get("agent", {}), s.get("automation", {})
    console.print(
        f"Agent [bold]{agent.get('name', 'generic')}[/] · models GM={agent.get('gm_model') or 'default'} "
        f"reviewer={agent.get('reviewer_model') or 'default'} routine={agent.get('routine_model') or 'default'} · "
        f"time zone {s.get('owner', {}).get('timezone', 'UTC')} · league {s.get('league', {}).get('source', 'demo')}"
        f" · yahoo route {s.get('yahoo', {}).get('route') or 'auto'}"
        f" · data {'Hashtag Premium' if s.get('hashtag', {}).get('enabled') else 'free ESPN engine'}"
        f" · alerts {', '.join(wiz.effective_channels(s.get('notify', {}))) or 'none'}"
        f" · automation {auto.get('mode', 'none')} {', '.join(auto.get('jobs', []))}"
    )
    console.print("\n[bold]Next steps[/]")
    for i, step in enumerate(report.next_steps(), 1):
        console.print(f"  {i}. {step}")


@app.command("setup")
def setup_cmd(
    agent: str = typer.Option(None, help=f"Your AI agent: {' | '.join(AGENTS)}."),
    gm_model: str = typer.Option(None, help="Model for GM decisions (as your agent names it)."),
    reviewer_model: str = typer.Option(None, help="Model for independent review (ideally another family)."),
    routine_model: str = typer.Option(None, help="Cheap model for routine runs."),
    timezone: str = typer.Option(None, help="Your IANA time zone (default: detected)."),
    automation: str = typer.Option(None, help=f"Automation mode: {' | '.join(AUTOMATION_MODES)}."),
    jobs: str = typer.Option(None, help=f"Comma-separated jobs: {','.join(JOBS)}."),
    league_source: str = typer.Option(None, help="demo | yahoo."),
    email: bool = typer.Option(None, "--email/--no-email", help="Gmail notifications."),
    quiet_hours: str = typer.Option(None, help='Local quiet hours, e.g. "23:00-07:00" ("" = none).'),
    expiry_hours: float = typer.Option(None, help="Hours before an unapproved proposal expires."),
    yahoo_approved: bool = typer.Option(False, help="Yahoo API access is approved (show/run the connect steps)."),
    yahoo_route: str = typer.Option(
        None,
        help="How FBM reads Yahoo: flaim (recommended, free hosted read-only connector) | session (your Yahoo "
        "sign-in, `fbm yahoo login`) | api (your approved Yahoo app, `fbm auth`) | auto (session if saved, else api).",
    ),
    telegram: bool = typer.Option(
        None,
        "--telegram/--no-telegram",
        help="Telegram alerts (adds/removes telegram in the notify channels; then run `fbm notify setup`).",
    ),
    hashtag: bool = typer.Option(
        None,
        "--hashtag/--no-hashtag",
        help="Optional paid upgrade: Hashtag Basketball Premium as the data layer (real Yahoo league only). "
        "Off = FBM's free ESPN engine (fully supported).",
    ),
    hashtag_league_id: str = typer.Option(None, help="Hashtag league id (default: your Yahoo league id)."),
    skip_data: bool = typer.Option(False, help="Skip the NBA data backfill/sync."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive: use flags + existing/default values."),
    check: bool = typer.Option(False, "--check", help="Only re-run verification (writes nothing)."),
) -> None:
    """Set up (or re-check) FBM: settings, memory, NBA data, agent files, automation, health. Safe to re-run."""
    if check:
        report = wiz.check()
        _print_report(report)
        raise typer.Exit(1 if report.failed else 0)
    opts = wiz.SetupOptions(
        agent=agent,
        gm_model=gm_model,
        reviewer_model=reviewer_model,
        routine_model=routine_model,
        timezone=timezone,
        automation=automation,
        jobs=wiz.parse_jobs(jobs),
        league_source=league_source,
        email=email,
        quiet_hours=quiet_hours,
        expiry_hours=expiry_hours,
        skip_data=skip_data,
        yahoo_approved=yahoo_approved,
        hashtag=hashtag,
        hashtag_league_id=hashtag_league_id,
        yahoo_route=yahoo_route.strip().lower() if yahoo_route else None,
        telegram=telegram,
    )
    interactive = not yes and sys.stdin.isatty()
    current = wiz.current_values()
    if interactive:
        _interactive(opts, current)
    elif opts.timezone is None and current.get("owner", {}).get("timezone", "UTC") == "UTC":
        opts.timezone = wiz.detect_timezone()
    errors = wiz.validate(opts)
    if errors:
        for e in errors:
            console.print(f"[red]✗ {e}[/]")
        raise typer.Exit(2)
    if interactive:
        mode = opts.automation or "none"
        if mode != "none":
            console.print("\n[bold]Automation preview[/] (dry run):")
            preview = wiz.run_automation_install(wiz.default_runner, mode, opts.jobs or [], dry_run=True)
            console.print(f"  {preview.status}: {preview.detail}")
            if not typer.confirm("Install this automation?", default=True):
                opts.automation = "none"

    def show(r: wiz.StepResult) -> None:
        console.print(f"[{_COLORS[r.status]}]{r.status:>7}[/] {r.step}: {r.detail}")

    report = wiz.run_setup(opts, on_step=show)
    # flaim_login / yahoo_web_session are set only by _ask_connect (on the demo, or a connected league moving to a new
    # route); yahoo_approved can also come from --yahoo-approved, so a connected league only runs it to move to api.
    on_yahoo = report.settings.get("league", {}).get("source") == "yahoo"
    if interactive and opts.flaim_login:
        _flaim_interactive()
        report = wiz.check()
    elif interactive and opts.yahoo_approved and (not on_yahoo or opts.yahoo_route == "api"):
        _yahoo_interactive()
        report = wiz.check()
    elif interactive and opts.yahoo_web_session:
        _fbm("yahoo", "login")
        report = wiz.check()
    if interactive and report.settings.get("league", {}).get("source") == "yahoo" and opts.hashtag is not False:
        _hashtag_interactive(opts, wiz.current_values())
        report = wiz.check()
    if interactive and opts.notify_setup and not _fbm("notify", "setup"):
        console.print("[red]`fbm notify setup` failed; fix it and run it again (see docs/telegram.md).[/]")
    console.print()
    _print_report(report)
    raise typer.Exit(1 if report.failed else 0)


@app.command("backup")
def backup_cmd(
    dest: str = typer.Option(None, help="Folder for the zip (default data/backups/, gitignored)."),
) -> None:
    """Zip memory/, reports/, settings, league config and the NBA database to a local folder (no secrets)."""
    from fbm.backup import backup

    path = backup(Path(dest) if dest else None)
    console.print(f"[green]Backup written[/] {path}")


@app.command("sync-memory", hidden=True)
def sync_memory_cmd(
    message: str = typer.Option("", "-m", "--message", help="Ignored (kept for compatibility)."),
    push: bool = typer.Option(True, help="Ignored (kept for compatibility)."),
) -> None:
    """Deprecated: memory is local-only. Use `fbm backup`."""
    from fbm.backup import LOCAL_ONLY_MESSAGE

    console.print(f"[yellow]{LOCAL_ONLY_MESSAGE}[/]")
