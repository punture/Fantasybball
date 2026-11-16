"""Which way FBM reads the Yahoo league: `[yahoo] route` in config/settings.toml (env FBM_YAHOO_ROUTE overrides).

- auto: the saved Yahoo web session (`fbm yahoo login`) if there is one, else the official API (`fbm auth`).
- flaim: Flaim's free, read-only MCP server (`fbm flaim login`): no Yahoo API approval or web session needed.
- session: only the Yahoo web session.
- api: only the official Yahoo API.

Every route is read-only. `league_client()` returns what the `fbm.yahoo` fetch functions take: a YahooClient, or a
FlaimLeague (which those functions route to).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from fbm.config import NotConfiguredError
from fbm.settings import EXAMPLE_PATH, SETTINGS_PATH, YAHOO_ROUTES, Settings, load_settings

ENV = "FBM_YAHOO_ROUTE"
LABELS = {
    "auto": "Yahoo web session if saved, else the official API",
    "flaim": "Flaim (free read-only MCP server)",
    "session": "Yahoo web session",
    "api": "official Yahoo API",
}


def yahoo_route(settings: Settings | None = None) -> str:
    route = (os.environ.get(ENV, "").strip() or (settings or load_settings()).yahoo_route or "auto").lower()
    if route not in YAHOO_ROUTES:
        raise NotConfiguredError(
            f'Unknown [yahoo] route "{route}" in config/settings.toml (or {ENV}): use one of {", ".join(YAHOO_ROUTES)}.'
        )
    return route


def is_flaim(settings: Settings | None = None) -> bool:
    try:
        return yahoo_route(settings) == "flaim"
    except NotConfiguredError:
        return False


def save_route(route: str, path: Path = SETTINGS_PATH, example: Path = EXAMPLE_PATH) -> None:
    """Set `[yahoo] route` in config/settings.toml, keeping every other value and the example's comments."""
    from fbm import setup_wizard as wiz

    if route not in YAHOO_ROUTES:
        raise NotConfiguredError(f'Unknown route "{route}": use one of {", ".join(YAHOO_ROUTES)}.')
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    values = wiz.merged_settings(existing, wiz.SetupOptions())
    values.setdefault("yahoo", {})["route"] = route
    text = wiz.render_settings(example.read_text(encoding="utf-8"), values)
    tomllib.loads(text)  # never write a file FBM can't read back
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def league_client(route: str | None = None) -> Any:
    """A read client for the active route (close it, or use it as a context manager)."""
    route = route or yahoo_route()
    if route == "flaim":
        from fbm.flaim.reads import FlaimLeague

        return FlaimLeague.from_config()
    from fbm.yahoo.client import YahooAPIError, YahooClient

    if route == "session":
        from fbm.yahoo import session as web_session

        if web_session.load() is None:
            raise YahooAPIError(401, "no Yahoo web session saved: run `fbm yahoo login` (or change [yahoo] route)", "")
        return YahooClient()  # picks the saved session and keeps the cookies Yahoo rotates
    if route == "api":
        from fbm.yahoo.auth import TokenManager

        return YahooClient(tokens=TokenManager())
    return YahooClient()
