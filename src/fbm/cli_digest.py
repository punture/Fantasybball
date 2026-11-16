"""`fbm digest` and `fbm alert lineup|injuries`: deterministic briefs and alerts from FBM's engine (no AI).

stdout carries only the message, so an assistant such as OpenClaw can relay it as-is; an alert prints exactly
NO_REPLY when there is nothing new. --send also delivers through the notify dispatcher ([notify] channels).
Exit 0 on success (NO_REPLY included); exit 1 with a short message on stderr on failure.
Registered by importing this module from fbm.cli.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn

import typer

from fbm.cli import app

alert_app = typer.Typer(no_args_is_help=True, help="Deterministic alerts (NO_REPLY when there is nothing new).")
app.add_typer(alert_app, name="alert")


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _deliver(subject: str, text: str, kind: str) -> None:
    from fbm.notify import dispatch
    from fbm.notify.base import NotifyError

    try:
        result = dispatch.send(subject, text, kind, title=False)
    except NotifyError as err:
        _fail(str(err))
    if result["status"] != "sent":
        failed = [d["detail"] for d in result.get("deliveries", []) if d["status"] != "sent"]
        typer.echo(f"notify: {result['status']} ({'; '.join(failed) or result.get('detail', '')})", err=True)


def _release_held() -> None:
    """Nothing to say this run: still deliver messages held during quiet hours, once they are over."""
    from fbm.notify import dispatch

    try:
        dispatch.flush_held()
    except Exception as err:  # noqa: BLE001 - the alert itself succeeded
        typer.echo(f"notify: held messages not released ({type(err).__name__})", err=True)


def _owner_zone():
    from fbm.notify.text import zone
    from fbm.settings import load_settings

    return zone(load_settings().owner_timezone)


@app.command("digest")
def digest_cmd(
    kind: str = typer.Option("daily", "--kind", help="daily (today's actions) | weekly (Monday matchup preview)."),
    send: bool = typer.Option(False, "--send", help="Also deliver it through [notify] channels."),
) -> None:
    """Compact Telegram-ready brief from FBM's engine (no AI): verdict first, at most 3500 characters.
    With --send (scheduled runs) the lineup/streaming decisions are logged under the daily-gm / weekly-plan trigger."""
    from fbm.notify import digest
    from fbm.timeutil import nba_today

    if kind not in digest.KINDS:
        _fail(f"--kind must be one of {', '.join(digest.KINDS)}")
    now = datetime.now(UTC)
    try:
        text = digest.build(kind, record=send, now=now)
    except Exception as err:  # noqa: BLE001 - reported on stderr, exit 1
        _fail(f"{type(err).__name__}: {err}")
    typer.echo(text)
    if send:
        _deliver(digest.subject(kind, nba_today(now)), text, "report")


@alert_app.command("lineup")
def alert_lineup(send: bool = typer.Option(False, "--send", help="Also deliver it through [notify] channels.")) -> None:
    """Lineup alert when the first affected tip-off is within ~90 minutes: a starter Out (or GTD) with a game, a
    bench player with a game who should start, or a changed recommendation. Exact manual steps (FBM never changes
    Yahoo). Prints NO_REPLY otherwise; each recommendation is sent once per ET game day. Run every 15 minutes."""
    from fbm.notify import alerts
    from fbm.notify import state as notify_state
    from fbm.notify.text import NO_REPLY
    from fbm.store import db

    conn = db.connect()
    try:
        text, new_state = alerts.lineup_alert(conn, datetime.now(UTC), _owner_zone())
    except alerts.AlertError as err:  # suggestions paused (Hashtag unhealthy): nothing to say, not a failure
        typer.echo(f"note: {err}", err=True)
        text, new_state = None, None
    except Exception as err:  # noqa: BLE001 - reported on stderr, exit 1
        _fail(f"{type(err).__name__}: {err}")
    if text is None or new_state is None:
        typer.echo(NO_REPLY)
        if send:
            _release_held()
        return
    typer.echo(text)
    if send:
        _deliver("Lineup alert", text, "alert")
    notify_state.put(conn, alerts.LINEUP_KEY, new_state)


@alert_app.command("injuries")
def alert_injuries(
    send: bool = typer.Option(False, "--send", help="Also deliver it through [notify] channels."),
) -> None:
    """New injury/status changes on your roster and your opponent's, plus new 'next man up' / role-change pickups
    among free agents, each with a one-line what to do. Prints NO_REPLY when nothing is new since the last run."""
    from fbm.notify import alerts
    from fbm.notify import state as notify_state
    from fbm.notify.text import NO_REPLY
    from fbm.store import db

    conn = db.connect()
    try:
        text, new_state = alerts.injury_alert(conn, datetime.now(UTC), _owner_zone())
    except Exception as err:  # noqa: BLE001 - reported on stderr, exit 1
        _fail(f"{type(err).__name__}: {err}")
    if text is None:
        typer.echo(NO_REPLY)
        if send:
            _release_held()
    else:
        typer.echo(text)
        if send:
            _deliver("Injury update", text, "alert")
    notify_state.put(conn, alerts.INJURY_KEY, new_state)
