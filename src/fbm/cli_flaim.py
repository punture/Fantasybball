"""`fbm flaim`: read the Yahoo league through Flaim (free, read-only; no Yahoo API approval). Registered by importing
this module from fbm.cli."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import typer
from rich.table import Table

from fbm.cli import _fail, _print_leagues, app, console
from fbm.config import LEAGUE_CONFIG_PATH, LeagueConfig, NotConfiguredError, load_league_config, save_league_config
from fbm.flaim import check as flaim_check
from fbm.flaim import oauth
from fbm.flaim.errors import FlaimError
from fbm.flaim.reads import FlaimLeague, discovery_row
from fbm.flaim.route import LABELS, save_route, yahoo_route
from fbm.settings import YAHOO_ROUTES, load_settings
from fbm.timeutil import nba_today, parse_date

flaim_app = typer.Typer(
    no_args_is_help=True,
    help="Flaim: read your Yahoo league through Flaim's free, read-only service (no Yahoo API approval needed).",
)
app.add_typer(flaim_app, name="flaim")

INTRO = (
    "Flaim (flaim.app) is a free, read-only service that connects your Yahoo league with its own approved Yahoo "
    "app. FBM signs in to Flaim once; Flaim keeps your Yahoo connection (you connect Yahoo at flaim.app/leagues). "
    "Nobody can change your league through it. FBM keeps its Flaim sign-in in your OS credential store, never in a "
    "file or the AI chat.\n"
)


def _local(iso: str | None) -> str:
    """A UTC ISO time in the owner's time zone ([owner] timezone)."""
    if not iso:
        return "-"
    try:
        zone = ZoneInfo(load_settings().owner_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    return datetime.fromisoformat(iso).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _reader(**kwargs) -> FlaimLeague:
    from fbm.flaim.client import FlaimClient

    try:
        client = FlaimClient(**kwargs)
    except FlaimError as err:  # not signed in, or the sign-in lapsed
        _fail(str(err))
    except Exception as err:  # noqa: BLE001 - e.g. no usable OS credential store
        _fail(f"Couldn't read the Flaim sign-in from your OS credential store ({type(err).__name__}: {err}).")
    return FlaimLeague.from_config(client)


def _choose_league(reader: FlaimLeague, league_id: str | None) -> LeagueConfig:
    refs = reader.leagues_refs()
    if not refs:
        _fail(
            "Flaim lists no Yahoo NBA league for your account. Connect Yahoo at https://flaim.app/leagues, then run "
            "`fbm flaim leagues --select`."
        )
    if league_id:
        chosen = next((r for r in refs if r.league_key == league_id or r.league_key.endswith(f".l.{league_id}")), None)
        if chosen is None:
            _fail(f"League {league_id} isn't among your Flaim leagues (`fbm flaim leagues` lists them).")
    elif len(refs) == 1:
        chosen = refs[0]
    else:
        _print_leagues([discovery_row(r) for r in refs])
        idx = typer.prompt("Pick the league number to manage", type=int)
        if not 1 <= idx <= len(refs):
            _fail("Invalid selection.")
        chosen = refs[idx - 1]
    if not chosen.team_key:
        _fail(f"Flaim doesn't say which team is yours in {chosen.name}: re-connect Yahoo at https://flaim.app/leagues.")
    cfg = LeagueConfig(
        game_key=chosen.game_key,
        season=str(chosen.season_year or ""),
        league_key=chosen.league_key,
        league_name=chosen.name,
        team_key=chosen.team_key,
        team_name=chosen.team_name,
    )
    save_league_config(cfg)
    console.print(
        f"[green]Managing[/] [bold]{cfg.team_name}[/] in [bold]{cfg.league_name}[/] ({cfg.league_key}). "
        f"Saved to {LEAGUE_CONFIG_PATH.name}."
    )
    return cfg


def _use_live_league() -> None:
    from fbm import setup_wizard as wiz

    if load_settings().league_source == "yahoo":
        console.print("Next: `fbm flaim check` (verifies every feed), then `fbm snapshot`.")
        return
    if not sys.stdin.isatty() or not typer.confirm("Switch FBM from the demo league to your Yahoo league now?", True):
        console.print("When ready: `fbm setup --league-source yahoo --yes`, then `fbm flaim check` and `fbm snapshot`.")
        return
    wiz.write_settings(wiz.SetupOptions(league_source="yahoo"))
    console.print('[green]League source set to "yahoo".[/]')
    for args in (["settings"], ["snapshot"], ["ids"]):
        subprocess.run(wiz.fbm_command(*args), check=False)
    console.print("Run `fbm flaim check` any time to verify every Flaim feed.")


@flaim_app.command("login")
def flaim_login(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the sign-in address instead of opening a browser (e.g. on a headless box)."
    ),
    league_id: str = typer.Option(None, "--league-id", help="League to manage, e.g. 466.l.12345 (skips the question)."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the browser sign-in."),
) -> None:
    """Sign in to Flaim once, pick your Yahoo league, and make Flaim the way FBM reads it."""
    console.print(INTRO)
    url = load_settings().flaim_url
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": "fbm/0.1"}) as http:
            state = _sign_in(http, url, no_browser, timeout)
    except FlaimError as err:
        _fail(f"{err.description} Nothing saved.")
    try:
        oauth.save(state)
    except Exception as err:  # noqa: BLE001 - e.g. no usable OS credential store
        _fail(f"Signed in, but couldn't save the sign-in to your OS credential store ({type(err).__name__}: {err}).")
    console.print("[green]Signed in to Flaim.[/] The sign-in is in your OS credential store.")
    with _reader(url=url) as reader:
        try:
            _choose_league(reader, league_id)
        except FlaimError as err:
            _fail(str(err))
    save_route("flaim")
    console.print('[green]FBM now reads your league through Flaim[/] (\\[yahoo] route = "flaim").')
    _use_live_league()


def _sign_in(http: httpx.Client, url: str, no_browser: bool, timeout: int) -> oauth.OAuthState:
    from fbm.flaim.login import sign_in

    return sign_in(
        http,
        url,
        open_browser=not no_browser,
        echo=console.print,
        prompt=lambda q: typer.prompt(q, default="", show_default=False),
        timeout_s=timeout,
    )


@flaim_app.command("logout")
def flaim_logout() -> None:
    """Forget FBM's Flaim sign-in (your Flaim account and its Yahoo connection stay as they are)."""
    oauth.clear()
    console.print("Flaim sign-in removed from your OS credential store.")
    if yahoo_route() == "flaim":
        console.print(
            '[yellow]\\[yahoo] route is still "flaim":[/] sign in again with `fbm flaim login`, or switch with '
            "`fbm flaim route session` (Yahoo web session) or `fbm flaim route api` (official API)."
        )


@flaim_app.command("status")
def flaim_status() -> None:
    """Route, Flaim sign-in and league (never prints secrets)."""
    try:
        route = yahoo_route()
        console.print(f"Route: [bold]{route}[/] ({LABELS[route]})")
    except NotConfiguredError as err:
        console.print(f"[red]{err}[/]")
    try:
        state = oauth.load()
    except Exception as err:  # noqa: BLE001 - e.g. no usable OS credential store
        state = None
        console.print(f"[red]OS credential store unreadable[/] ({type(err).__name__}: {err})")
    if state is None:
        console.print("Flaim: [yellow]not signed in[/] (`fbm flaim login`)")
    else:
        s = state.summary()
        mark = "[green]signed in[/]" if s["signed_in"] else "[red]sign-in lapsed[/] (`fbm flaim login`)"
        console.print(
            f"Flaim: {mark} since {_local(s['signed_in_utc'])} · access token valid until "
            f"{_local(s['access_token_expires_utc'])} · sign-in lapses {_local(s['refresh_window_ends_utc'])} "
            "unless FBM reads the league before then (every read renews it)"
        )
    try:
        cfg = load_league_config()
        console.print(f"League: [bold]{cfg.league_name}[/] ({cfg.league_key}) · Team: [bold]{cfg.team_name}[/]")
    except NotConfiguredError:
        console.print("League: [yellow]not selected[/] (`fbm flaim leagues --select`)")
    from fbm.flaim.league import last_warnings

    last = last_warnings()
    if last:
        console.print(
            f"Last Flaim snapshot {_local(last.get('fetched_at'))}: week window {' .. '.join(last['week_window'])} "
            f"({last.get('week_source')}), {len(last.get('warnings', []))} warning(s) (`fbm flaim check` lists them)"
        )


@flaim_app.command("leagues")
def flaim_leagues(
    select: bool = typer.Option(False, "--select", help="Choose the league FBM manages."),
    league_id: str = typer.Option(None, "--league-id", help="With --select: the league key, e.g. 466.l.12345."),
) -> None:
    """Your Yahoo NBA leagues on Flaim."""
    with _reader() as reader:
        try:
            if select or league_id:
                _choose_league(reader, league_id)
            else:
                _print_leagues(reader.leagues())
        except FlaimError as err:
            _fail(str(err))


@flaim_app.command("route")
def flaim_route(
    name: str = typer.Argument(None, help=f"New route: {' | '.join(YAHOO_ROUTES)} (omit to show the current one)."),
) -> None:
    """Show or set how FBM reads Yahoo (the route setting in config/settings.toml)."""
    if name is None:
        route = yahoo_route()
        console.print(f"Route: [bold]{route}[/] ({LABELS[route]})")
        return
    try:
        save_route(name.strip().lower())
    except NotConfiguredError as err:
        _fail(str(err))
    console.print(f"[green]Route set:[/] {name} ({LABELS[name.strip().lower()]}). Next: `fbm snapshot`.")


def _print_report(report: flaim_check.Report, title: str) -> None:
    table = Table(title=title)
    for col in ("Feed", "", "Detail", "Used for"):
        table.add_column(col)
    marks = {
        flaim_check.OK: "[green]OK[/]",
        flaim_check.WARN: "[yellow]WARN[/]",
        flaim_check.FAIL: "[red]FAIL[/]",
        flaim_check.SKIP: "[dim]skip[/]",
    }
    for f in report.feeds:
        table.add_row(f.name, marks[f.status], f.detail, f.used_for)
    console.print(table)
    if report.unmatched:
        console.print(f"[yellow]{len(report.unmatched)} Yahoo players without an ESPN id[/] (`fbm ids` to fix):")
        for p in report.unmatched[:20]:
            console.print(f"  {p['yahoo_id']}: {p['name']} ({p['team']})")
    if report.warnings:
        console.print(f"[bold]Warnings[/] ({len(report.warnings)}):")
        for w in report.warnings:
            console.print(f"  - {w}")


def _check(reader: FlaimLeague, day: str | None) -> flaim_check.Report:
    from fbm.store import db

    try:
        cfg = load_league_config()
    except NotConfiguredError:
        cfg = None
    conn = db.connect()
    try:
        return flaim_check.run(reader, cfg, parse_date(day) if day else nba_today(), conn)
    finally:
        conn.close()


@flaim_app.command("check")
def flaim_check_cmd(
    day: str = typer.Option(None, "--date", help="ET game day for the roster reads (default: today ET)."),
) -> None:
    """Read every Flaim feed FBM uses and report what works, what's missing and any mapping misses. Read-only."""
    with _reader() as reader:
        report = _check(reader, day)
    _print_report(report, "Flaim data feeds")
    ok = sum(f.status == flaim_check.OK for f in report.feeds)
    console.print(f"{ok}/{len(report.feeds)} feeds fully OK. `fbm flaim dump` saves the raw responses to share.")
    if report.failed:
        raise typer.Exit(code=1)


@flaim_app.command("dump")
def flaim_dump(
    out: str = typer.Option(None, "--out", help="Folder for the files (default data/raw/flaim/<UTC time>/)."),
    day: str = typer.Option(None, "--date", help="ET game day for the roster reads (default: today ET)."),
) -> None:
    """Save every raw Flaim response (plus the check report) as JSON, zipped, to fix mappings against real data."""
    target = Path(out) if out else flaim_check.dump_dir()
    recorder = flaim_check.Recorder(target)
    with _reader(recorder=recorder) as reader:
        report = _check(reader, day)
    target.mkdir(parents=True, exist_ok=True)
    summary = {
        "feeds": [asdict(f) for f in report.feeds],
        "warnings": report.warnings,
        "unmatched": report.unmatched,
    }
    (target / "check_report.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    archive = shutil.make_archive(str(target), "zip", root_dir=target)
    _print_report(report, "Flaim data feeds")
    console.print(
        f"[green]Saved {len(recorder.paths)} responses[/] to {target} and {archive}.\n"
        "They contain your league's data (team names, managers' nicknames, rosters) but no passwords or tokens. "
        "Share the zip only with someone you trust."
    )
