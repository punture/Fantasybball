"""`fbm notify setup` helpers: parse the channel list and write [notify] keys into config/settings.toml (with FBM's
settings writer, keeping the example's comments). Secrets never go here: they live in the credential store."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from fbm.settings import EXAMPLE_PATH, NOTIFY_CHANNELS, SETTINGS_PATH

CHAT_ID_RE = re.compile(r"^-?\d{3,20}$")


def parse_channels(text: str) -> list[str]:
    """'telegram, email' -> ['telegram', 'email'] (order kept, duplicates dropped; 'none' = no channel)."""
    names = [c.strip().lower() for c in re.split(r"[,\s]+", text or "") if c.strip()]
    names = [n for n in names if n != "none"]
    unknown = [n for n in names if n not in NOTIFY_CHANNELS]
    if unknown:
        raise ValueError(f"Unknown channel(s) {', '.join(unknown)}; choose from {', '.join(NOTIFY_CHANNELS)}.")
    return list(dict.fromkeys(names))


def save_notify_settings(values: dict[str, Any], path: Path = SETTINGS_PATH, example: Path = EXAMPLE_PATH) -> str:
    """Merge `values` into [notify] of the settings file (created from the example if missing). Returns a short
    status line."""
    from fbm.setup_wizard import render_settings

    existing = path.read_text(encoding="utf-8") if path.exists() else None
    merged: dict[str, dict[str, Any]] = {}
    if existing:
        merged = {sec: dict(kv) for sec, kv in tomllib.loads(existing).items() if isinstance(kv, dict)}
    merged.setdefault("notify", {}).update(values)
    text = render_settings(example.read_text(encoding="utf-8"), merged)
    tomllib.loads(text)  # never write a file FBM can't read back
    if text == existing:
        return f"{path.name} unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"{'updated' if existing else 'created'} {path.name}"
