"""Small persisted key/value state for notifications (alert dedupe), in FBM's SQLite DB (table notify_state)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fbm.store import db

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS notify_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


def get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    conn.execute(_SCHEMA)
    row = conn.execute("SELECT value FROM notify_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def put(conn: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
    conn.execute(_SCHEMA)
    conn.execute(
        "INSERT INTO notify_state(key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, sort_keys=True), db.now_iso()),
    )
    conn.commit()
