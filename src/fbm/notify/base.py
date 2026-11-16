"""Shared notification plumbing: the error type, urgent kinds, quiet hours and the outbox log."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fbm.settings import Settings
from fbm.store import db

URGENT_KINDS = ("approval", "alert")  # delivered even during quiet hours


class NotifyError(RuntimeError):
    pass


def in_quiet_hours(window: str, now: datetime | None = None) -> bool:
    """window like '23:00-07:00' (wall-clock time of `now`); wraps midnight."""
    if not window or "-" not in window:
        return False
    start_s, end_s = (w.strip() for w in window.split("-", 1))
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    current = (now or datetime.now()).time()
    return start <= current < end if start < end else current >= start or current < end


def owner_now(settings: Settings, now: datetime | None = None) -> datetime:
    """`now` (default: the current time) in the owner's time zone ([owner] timezone); machine-local if unknown."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    try:
        return moment.astimezone(ZoneInfo(settings.owner_timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return moment.astimezone()


def held_for_quiet_hours(settings: Settings, kind: str, now: datetime | None = None) -> bool:
    """Non-urgent kinds wait while the owner's quiet hours are on."""
    return kind not in URGENT_KINDS and in_quiet_hours(settings.quiet_hours, owner_now(settings, now))


def log_outbox(conn: sqlite3.Connection, channel: str, subject: str, body: str, status: str, detail: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO outbox(created_at, channel, subject, body, status, detail) VALUES (?,?,?,?,?,?)",
        (db.now_iso(), channel, subject, body, status, detail[:300]),
    )
    conn.commit()
    return int(cur.lastrowid or 0)
