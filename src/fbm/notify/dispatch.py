"""Notification dispatcher: one message to every channel enabled in `[notify] channels`.

Routes: "openclaw" and "telegram" both reach the owner's Telegram chat, so together they are ONE route (OpenClaw
first, direct Telegram only if OpenClaw fails: delivered once, never twice); "email" (Gmail) is its own route.
Non-urgent kinds are held during the owner's quiet hours (logged 'held') and released by the next send outside them
(if at most HELD_MAX_AGE_H old; also `fbm notify flush`). Non-urgent chat messages arrive silently (no sound).
Every attempt is logged to the outbox table (channel telegram | openclaw | email, with its status).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fbm.notify import email, openclaw, telegram
from fbm.notify.base import URGENT_KINDS, NotifyError, held_for_quiet_hours, log_outbox
from fbm.settings import NOTIFY_CHANNELS, Settings, load_settings
from fbm.store import db

CHAT_ORDER = ("openclaw", "telegram")  # same Telegram chat: OpenClaw first, direct Bot API as the fallback
HELD_MAX_AGE_H = 12

# (subject, text, silent, settings) -> short delivery detail; raises NotifyError on failure.
Transport = Callable[[str, str, bool, Settings], str]


def _telegram(subject: str, text: str, silent: bool, settings: Settings) -> str:
    return f"{telegram.send(text, silent=silent, settings=settings)} message(s)"


def _openclaw(subject: str, text: str, silent: bool, settings: Settings) -> str:
    return f"{openclaw.send(text, silent=silent, settings=settings)} message(s)"


def _email(subject: str, text: str, silent: bool, settings: Settings) -> str:
    return f"to {email.deliver(subject, text, settings)}"


DEFAULT_TRANSPORTS: dict[str, Transport] = {"telegram": _telegram, "openclaw": _openclaw, "email": _email}


def routes(channels: Sequence[str]) -> list[tuple[str, ...]]:
    """Delivery routes in channel order: the chat route (fallback order CHAT_ORDER) and/or email."""
    out: list[tuple[str, ...]] = []
    for ch in channels:
        if ch in CHAT_ORDER:
            route = tuple(c for c in CHAT_ORDER if c in channels)
        elif ch in NOTIFY_CHANNELS:
            route = (ch,)
        else:
            continue
        if route not in out:
            out.append(route)
    return out


def chat_text(subject: str, body: str, title: bool = True) -> str:
    """What a chat channel receives: the subject as a bold first line (unless the body carries its own title)."""
    return f"**{subject}**\n\n{body}" if title else body


def _text(route: tuple[str, ...], subject: str, body: str, title: bool) -> str:
    return body if route == ("email",) else chat_text(subject, body, title)


def _deliver(
    conn: sqlite3.Connection,
    route: tuple[str, ...],
    subject: str,
    text: str,
    silent: bool,
    settings: Settings,
    transports: Mapping[str, Transport],
) -> dict[str, Any]:
    """Try the route's channels in order until one delivers; log every attempt."""
    errors: list[str] = []
    for ch in route:
        try:
            detail = transports[ch](subject, text, silent, settings)
        except NotifyError as err:
            problem = str(err)
        except Exception as err:  # noqa: BLE001 - an unexpected bug must not block the fallback; never echo it
            problem = f"unexpected {type(err).__name__}"
        else:
            note = f" (fallback: {errors[-1]})" if errors else ""
            log_outbox(conn, ch, subject, text, "sent", detail + note)
            return {"channel": ch, "status": "sent", "detail": detail, **({"fallback_from": errors} if errors else {})}
        log_outbox(conn, ch, subject, text, "failed", problem)
        errors.append(f"{ch}: {problem}")
    return {"channel": route[0], "status": "failed", "detail": "; ".join(errors)}


def flush_held(
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    transports: Mapping[str, Transport] | None = None,
) -> list[dict[str, Any]]:
    """Deliver messages held during quiet hours (at most HELD_MAX_AGE_H old) once quiet hours are over."""
    settings = settings or load_settings()
    if held_for_quiet_hours(settings, "report", now):
        return []
    conn = conn or db.connect()
    transports = transports or DEFAULT_TRANSPORTS
    plan = routes(settings.notify_channels)
    cutoff = ((now or datetime.now(UTC)).astimezone(UTC) - timedelta(hours=HELD_MAX_AGE_H)).isoformat("T", "seconds")
    rows = conn.execute(
        "SELECT id, channel, subject, body FROM outbox WHERE status = 'held' AND created_at >= ? ORDER BY id",
        (cutoff,),
    ).fetchall()
    released = []
    for row in rows:
        route = next((r for r in plan if row["channel"] in r), None)
        if route is None:
            continue  # that channel is no longer enabled
        claimed = conn.execute("UPDATE outbox SET status = 'releasing' WHERE id = ? AND status = 'held'", (row["id"],))
        conn.commit()
        if not claimed.rowcount:
            continue  # another run is releasing it
        subject = str(row["subject"]).removeprefix(email.SUBJECT_PREFIX)
        result = _deliver(conn, route, subject, row["body"], True, settings, transports)
        ok = result["status"] == "sent"
        conn.execute(
            "UPDATE outbox SET status = 'released', detail = ? WHERE id = ?",
            (
                f"delivered after quiet hours via {result['channel']}" if ok else "release failed (see later rows)",
                row["id"],
            ),
        )
        conn.commit()
        released.append(result)
    return released


def send(
    subject: str,
    body: str,
    kind: str = "report",
    *,
    title: bool = True,
    only: str | None = None,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    transports: Mapping[str, Transport] | None = None,
) -> dict[str, Any]:
    """Send a Markdown message to every enabled channel. kind: report | approval | alert | retro (approval and alert
    are urgent: never held, delivered with sound). title=False when `body` starts with its own title line.
    only: a single channel (no fallback), e.g. `fbm notify test --channel telegram`.
    Returns {status: sent | partial | held | disabled, ...}; raises NotifyError when nothing could be delivered."""
    settings = settings or load_settings()
    conn = conn or db.connect()
    transports = transports or DEFAULT_TRANSPORTS
    if only is not None and only not in NOTIFY_CHANNELS:
        raise NotifyError(f"Unknown channel {only!r}; use one of {', '.join(NOTIFY_CHANNELS)}.")
    plan = routes((only,) if only else settings.notify_channels)
    if not plan:
        log_outbox(conn, "none", subject, body, "disabled", "no channel in [notify] channels")
        return {"status": "disabled", "detail": "No notification channel enabled: run `fbm notify setup`."}
    urgent = kind in URGENT_KINDS
    if not only and held_for_quiet_hours(settings, kind, now):
        for route in plan:
            log_outbox(conn, route[0], subject, _text(route, subject, body, title), "held", "quiet hours")
        return {"status": "held", "detail": "quiet hours", "channels": [r[0] for r in plan]}
    released = [] if only else flush_held(conn, settings, now, transports)
    results = [
        _deliver(conn, route, subject, _text(route, subject, body, title), not urgent, settings, transports)
        for route in plan
    ]
    delivered = [r for r in results if r["status"] == "sent"]
    if not delivered:
        raise NotifyError("Notification not delivered: " + "; ".join(r["detail"] for r in results))
    out: dict[str, Any] = {"status": "sent" if len(delivered) == len(results) else "partial", "deliveries": results}
    if released:
        out["released_after_quiet_hours"] = len(released)
    return out


def readiness(settings: Settings) -> dict[str, tuple[bool, str]]:
    """Per enabled channel: (ready, detail). Presence checks only: never reads out a secret."""
    out: dict[str, tuple[bool, str]] = {}
    for ch in settings.notify_channels:
        try:
            out[ch] = _ready(ch, settings)
        except NotifyError as err:
            out[ch] = (False, str(err))
    return out


def _ready(channel: str, settings: Settings) -> tuple[bool, str]:
    chat = bool(settings.telegram_chat_id.strip())
    if channel == "telegram":
        missing = [m for m, ok in (("chat id", chat), ("bot token", bool(telegram.stored_token()))) if not ok]
        return (False, f"missing {' and '.join(missing)}") if missing else (True, "bot token stored, chat id set")
    if channel == "openclaw":
        argv = openclaw.resolve(settings.openclaw_bin)
        return (True, f"CLI found ({argv[-1]})") if chat else (False, "missing chat id")
    from fbm import credentials

    try:
        stored = all(credentials.get(n) for n in (credentials.GMAIL_ADDRESS, credentials.GMAIL_APP_PASSWORD))
    except Exception as err:  # noqa: BLE001 - e.g. no OS keyring on a headless machine
        return False, f"credential store unavailable ({type(err).__name__})"
    return (True, "Gmail app password stored") if stored else (False, "Gmail not configured")
