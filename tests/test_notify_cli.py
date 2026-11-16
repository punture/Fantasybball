"""`fbm notify setup|test|flush|outbox`: interactive Telegram/OpenClaw setup (secrets to the credential store only)."""

import pytest
from typer.testing import CliRunner

from fbm.cli import app
from fbm.notify import dispatch
from fbm.notify import setup as notify_setup
from fbm.settings import EXAMPLE_PATH, Settings, load_settings

TOKEN = "123456789:" + "B" * 35


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp settings file and an in-memory credential store."""
    path = tmp_path / "settings.toml"
    creds: dict[str, str] = {}
    real_save = notify_setup.save_notify_settings
    monkeypatch.setattr(notify_setup, "save_notify_settings", lambda values: real_save(values, path, EXAMPLE_PATH))
    monkeypatch.setattr("fbm.settings.load_settings", lambda: load_settings(path))
    monkeypatch.setattr("fbm.credentials.get", creds.get)
    monkeypatch.setattr("fbm.credentials.put", creds.__setitem__)
    return path, creds


def test_setup_telegram_interactively(env):
    path, creds = env
    out = CliRunner().invoke(app, ["notify", "setup"], input=f"telegram\n{TOKEN}\n123456789\n")
    assert out.exit_code == 0, out.output
    assert creds == {"telegram_bot_token": TOKEN}
    assert TOKEN not in out.output and TOKEN not in path.read_text()  # never echoed or written to settings
    s = load_settings(path)
    assert s.notify_channels == ("telegram",) and s.telegram_chat_id == "123456789" and not s.email_enabled
    assert "getUpdates" in out.output  # how to find the chat id


def test_setup_openclaw_with_fallback_keeps_the_stored_token(env, monkeypatch):
    path, creds = env
    creds["telegram_bot_token"] = TOKEN
    from fbm.notify import openclaw

    def missing(binary):
        raise openclaw.OpenClawError(f"OpenClaw CLI '{binary}' not found")

    monkeypatch.setattr(openclaw, "resolve", missing)
    args = ["notify", "setup", "--channels", "openclaw,telegram", "--chat-id", "-100777", "--openclaw-bin", "/x/oc"]
    out = CliRunner().invoke(app, args, input="\n")
    assert out.exit_code == 0, out.output
    assert "not found" in out.output and creds["telegram_bot_token"] == TOKEN
    s = load_settings(path)
    assert (s.notify_channels, s.telegram_chat_id, s.openclaw_bin) == (("openclaw", "telegram"), "-100777", "/x/oc")


def test_setup_rejects_bad_input(env):
    runner = CliRunner()
    assert runner.invoke(app, ["notify", "setup", "--channels", "sms"]).exit_code == 1
    out = runner.invoke(app, ["notify", "setup", "--channels", "telegram", "--chat-id", "@me"], input=f"{TOKEN}\n")
    assert out.exit_code == 1 and "not a Telegram chat id" in out.output
    out = runner.invoke(app, ["notify", "setup", "--channels", "telegram"], input="\n")
    assert out.exit_code == 1 and "@BotFather" in out.output  # no token typed and none stored


def test_setup_detect_chat_id_calls_get_updates_once(env, monkeypatch):
    path, _creds = env
    from fbm.notify import telegram

    calls = []
    monkeypatch.setattr(
        telegram,
        "detect_chat_ids",
        lambda token: calls.append(token) or [{"chat_id": "424242", "name": "me", "type": "private"}],
    )
    out = CliRunner().invoke(
        app, ["notify", "setup", "--channels", "telegram", "--detect-chat-id"], input=f"{TOKEN}\ny\n\n"
    )
    assert out.exit_code == 0, out.output
    assert calls == [TOKEN] and "OpenClaw" in out.output and load_settings(path).telegram_chat_id == "424242"


def test_notify_test_and_flush(monkeypatch):
    seen = []

    def fake_send(subject, body, kind="report", **kw):
        seen.append((kind, kw))
        return {"status": "sent", "deliveries": [{"channel": "telegram", "status": "sent", "detail": "1 message(s)"}]}

    monkeypatch.setattr(dispatch, "send", fake_send)
    out = CliRunner().invoke(app, ["notify", "test", "--channel", "telegram"])
    assert out.exit_code == 0 and "sent telegram: 1 message(s)" in out.output
    assert seen == [("alert", {"only": "telegram"})]
    monkeypatch.setattr(dispatch, "send", lambda *a, **k: {"status": "disabled", "detail": "No channel enabled."})
    assert CliRunner().invoke(app, ["notify", "test"]).exit_code == 1
    monkeypatch.setattr(dispatch, "flush_held", lambda: [{"channel": "telegram"}])
    out = CliRunner().invoke(app, ["notify", "flush"])
    assert out.exit_code == 0 and "1 held message(s) released" in out.output


def test_outbox_shows_the_channel(monkeypatch):
    from fbm.notify.base import log_outbox
    from fbm.store import db

    conn = db.connect(":memory:")
    log_outbox(conn, "openclaw", "Lineup alert", "x", "failed", "OpenClaw exited 1")
    monkeypatch.setattr("fbm.cli.store.connect", lambda: conn)
    out = CliRunner().invoke(app, ["notify", "outbox"])
    assert out.exit_code == 0 and "openclaw" in out.output and "failed" in out.output
    assert Settings().notify_channels == ()
