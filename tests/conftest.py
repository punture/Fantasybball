"""Suite-wide isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hashtag_off_by_default(monkeypatch):
    """The owner's config/settings.toml may enable Hashtag mode; tests opt in explicitly instead."""
    monkeypatch.setenv("FBM_HASHTAG_ENABLED", "0")
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "auto")  # likewise `[yahoo] route`: Flaim tests opt in explicitly


@pytest.fixture(autouse=True)
def _fresh_yahoo_week_cache():
    """Yahoo scoreboard-by-week lookups are cached per process; tests must not share them."""
    from fbm.gm import services

    services._WEEK_MATCHUPS.clear()
    yield
    services._WEEK_MATCHUPS.clear()


@pytest.fixture(autouse=True)
def _isolated_secret_file(monkeypatch, tmp_path):
    """Without an OS keyring, fbm.credentials falls back to a secrets file: keep it out of the real data/ folder."""
    from fbm import credentials

    monkeypatch.setenv(credentials.FILE_ENV, str(tmp_path / "secrets" / "credentials.json"))
    monkeypatch.delenv(credentials.BACKEND_ENV, raising=False)
    credentials.reset_backend()
    yield
    credentials.reset_backend()
