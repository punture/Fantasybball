"""`fbm dashboard`: where the Streamlit server listens."""

from __future__ import annotations

from typer.testing import CliRunner

from fbm import cli


def _option(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_dashboard_stays_on_this_computer_unless_an_address_is_given(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    assert CliRunner().invoke(cli.app, ["dashboard", "--no-open-browser"]).exit_code == 0
    assert CliRunner().invoke(cli.app, ["dashboard", "--address", "0.0.0.0", "--port", "8600"]).exit_code == 0
    local, network = calls
    assert _option(local, "--server.address") == "127.0.0.1"
    assert _option(local, "--server.headless") == "true"
    assert _option(network, "--server.address") == "0.0.0.0"
    assert _option(network, "--server.port") == "8600"
