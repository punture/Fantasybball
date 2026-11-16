"""`fbm data sync` must not crash on the post-sync summary (scheduled jobs treat a crash as a failed sync)."""

from typer.testing import CliRunner

from fbm import cli
from fbm.data import sync
from fbm.gm import services


def test_data_sync_prints_post_sync_summary_without_forecasts_key(monkeypatch):
    monkeypatch.setattr(cli.store, "connect", lambda: None)
    monkeypatch.setattr(cli, "EspnClient", lambda: None)
    monkeypatch.setattr(sync, "daily_sync", lambda conn, client, day, progress=None: ["box scores: 3 new"])
    signal = {"kind": "role_change", "direction": "up", "player": "A. Player", "nba": "BOS", "confidence": "med"}
    monkeypatch.setattr(
        services,
        "post_sync",
        lambda: {"signals_detected": 2, "signals_new": [{**signal, "note": "minutes up"}], "fa_radar": {}},
    )
    result = CliRunner().invoke(cli.app, ["data", "sync"])
    assert result.exit_code == 0, result.output
    assert "signals detected: 2" in result.output and "A. Player" in result.output
