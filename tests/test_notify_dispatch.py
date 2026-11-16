"""Notification dispatcher: routes and fallback, quiet hours (held + released later), outbox logging, settings,
setup helpers, health readiness and the send_email / notify tools."""

from datetime import UTC, datetime, timedelta

import pytest

from fbm.gm import health
from fbm.notify import dispatch
from fbm.notify.base import NotifyError, in_quiet_hours, owner_now
from fbm.notify.setup import parse_channels, save_notify_settings
from fbm.settings import EXAMPLE_PATH, Settings, load_settings
from fbm.store import db

NOON_UTC = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


class Transports(dict):
    """Fake channel transports: record calls; channels listed in `fail` raise NotifyError."""

    def __init__(self, fail=()):
        self.calls = []
        super().__init__({ch: self._make(ch, ch in fail) for ch in ("telegram", "openclaw", "email")})

    def _make(self, ch, fail):
        def transport(subject, text, silent, settings):
            self.calls.append((ch, subject, text, silent))
            if fail:
                raise NotifyError(f"{ch} down")
            return f"{ch} ok"

        return transport


def _outbox(conn):
    return [(r["channel"], r["status"]) for r in conn.execute("SELECT channel, status FROM outbox ORDER BY id")]


def test_routes_merge_openclaw_and_telegram_into_one_chat_route():
    assert dispatch.routes(("telegram", "openclaw", "email")) == [("openclaw", "telegram"), ("email",)]
    assert dispatch.routes(("email", "telegram")) == [("email",), ("telegram",)]
    assert dispatch.routes(("openclaw",)) == [("openclaw",)]
    assert dispatch.routes(()) == []


def test_openclaw_first_then_telegram_fallback_delivers_once(conn):
    s = Settings(notify_channels=("telegram", "openclaw"))
    ok = Transports()
    out = dispatch.send("Lineup alert", "body", "alert", conn=conn, settings=s, transports=ok, now=NOON_UTC)
    assert out["status"] == "sent" and [c[0] for c in ok.calls] == ["openclaw"]
    assert _outbox(conn) == [("openclaw", "sent")]

    down = Transports(fail=("openclaw",))
    out = dispatch.send("Lineup alert", "body", "alert", conn=conn, settings=s, transports=down, now=NOON_UTC)
    assert [c[0] for c in down.calls] == ["openclaw", "telegram"]
    assert out["deliveries"][0]["channel"] == "telegram" and out["deliveries"][0]["fallback_from"]
    assert _outbox(conn)[1:] == [("openclaw", "failed"), ("telegram", "sent")]


def test_chat_text_gets_the_subject_unless_the_body_has_a_title(conn):
    s = Settings(notify_channels=("telegram", "email"))
    t = Transports()
    dispatch.send("Daily", "hello", "alert", conn=conn, settings=s, transports=t, now=NOON_UTC)
    assert t.calls[0][2] == "**Daily**\n\nhello" and t.calls[1][2] == "hello"
    dispatch.send("Daily", "**Own title**", "alert", title=False, conn=conn, settings=s, transports=t)
    assert t.calls[2][2] == "**Own title**"


def test_urgent_kinds_ring_and_others_arrive_silently(conn):
    s = Settings(notify_channels=("telegram",))
    t = Transports()
    dispatch.send("a", "b", "alert", conn=conn, settings=s, transports=t, now=NOON_UTC)
    dispatch.send("a", "b", "report", conn=conn, settings=s, transports=t, now=NOON_UTC)
    assert [c[3] for c in t.calls] == [False, True]


def test_partial_and_total_failure(conn):
    s = Settings(notify_channels=("telegram", "email"))
    out = dispatch.send("a", "b", "alert", conn=conn, settings=s, transports=Transports(fail=("email",)))
    assert out["status"] == "partial"
    with pytest.raises(NotifyError, match="telegram down"):
        dispatch.send("a", "b", "alert", conn=conn, settings=s, transports=Transports(fail=("telegram", "email")))
    assert _outbox(conn)[-2:] == [("telegram", "failed"), ("email", "failed")]


def test_unexpected_transport_bugs_still_fall_back_without_details(conn):
    t = Transports()

    def boom(*a):
        raise ValueError("https://api.telegram.org/bot123:SECRET/sendMessage")

    t["openclaw"] = boom
    s = Settings(notify_channels=("openclaw", "telegram"))
    assert dispatch.send("a", "b", "alert", conn=conn, settings=s, transports=t)["status"] == "sent"
    detail = conn.execute("SELECT detail FROM outbox WHERE status = 'failed'").fetchone()[0]
    assert detail == "unexpected ValueError"


def test_disabled_and_single_channel_test(conn):
    out = dispatch.send("a", "b", conn=conn, settings=Settings(), transports=Transports())
    assert out["status"] == "disabled" and _outbox(conn) == [("none", "disabled")]
    t = Transports(fail=("openclaw",))
    s = Settings(notify_channels=("openclaw", "telegram"))
    with pytest.raises(NotifyError):  # --channel openclaw tests OpenClaw alone: no fallback
        dispatch.send("a", "b", "alert", only="openclaw", conn=conn, settings=s, transports=t)
    with pytest.raises(NotifyError, match="Unknown channel"):
        dispatch.send("a", "b", only="sms", conn=conn, settings=s, transports=t)


def test_quiet_hours_hold_non_urgent_and_release_them_later(conn):
    s = Settings(notify_channels=("openclaw", "telegram", "email"), quiet_hours="23:00-07:00", owner_timezone="UTC")
    night = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    t = Transports()
    held = dispatch.send(
        "Daily brief", "**FBM daily**", "report", title=False, conn=conn, settings=s, transports=t, now=night
    )
    assert held["status"] == "held" and t.calls == []
    assert _outbox(conn) == [("openclaw", "held"), ("email", "held")]
    urgent = dispatch.send("Lineup alert", "x", "alert", conn=conn, settings=s, transports=t, now=night)
    assert urgent["status"] == "sent" and _outbox(conn)[2:] == [("openclaw", "sent"), ("email", "sent")]
    assert dispatch.flush_held(conn, s, night, t) == []  # still quiet: nothing released

    morning = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    t2 = Transports(fail=("openclaw",))
    released = dispatch.flush_held(conn, s, morning, t2)
    assert [r["channel"] for r in released] == ["telegram", "email"]
    assert [c[0] for c in t2.calls] == ["openclaw", "telegram", "email"] and all(c[3] for c in t2.calls)
    assert t2.calls[1][2] == "**FBM daily**" and t2.calls[2][1] == "Daily brief"
    statuses = [r[0] for r in conn.execute("SELECT status FROM outbox ORDER BY id LIMIT 2")]
    assert statuses == ["released", "released"]
    assert dispatch.flush_held(conn, s, morning, t2) == []  # released once only


def test_held_messages_expire_and_send_releases_first(conn):
    s = Settings(notify_channels=("telegram",), quiet_hours="23:00-07:00")
    night = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
    dispatch.send("old", "x", conn=conn, settings=s, transports=Transports(), now=night)
    conn.execute("UPDATE outbox SET created_at = ?", ((night - timedelta(hours=13)).isoformat(),))
    dispatch.send("new", "y", conn=conn, settings=s, transports=Transports(), now=night)
    t = Transports()
    out = dispatch.send(
        "now", "z", "report", conn=conn, settings=s, transports=t, now=datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    )
    assert out["released_after_quiet_hours"] == 1
    assert [c[1] for c in t.calls] == ["new", "now"]  # the 13-hour-old one is not sent


def test_quiet_hours_use_the_owner_time_zone():
    s = Settings(quiet_hours="23:00-07:00", owner_timezone="America/Chicago")
    assert owner_now(s, NOON_UTC).hour == 6  # 12:00 UTC = 06:00 CST
    assert in_quiet_hours(s.quiet_hours, owner_now(s, NOON_UTC))
    assert owner_now(Settings(owner_timezone="Not/AZone"), NOON_UTC).tzinfo is not None


def test_settings_parse_channels(tmp_path):
    path = tmp_path / "settings.toml"
    channels = 'channels = ["Telegram", "openclaw", "sms", "telegram"]'
    path.write_text(f"[notify]\n{channels}\ntelegram_chat_id = 123\nemail_enabled = true\n")
    s = load_settings(path)
    assert s.notify_channels == ("telegram", "openclaw") and s.telegram_chat_id == "123"
    assert s.openclaw_bin == "openclaw"
    path.write_text("[notify]\nemail_enabled = true\n")
    assert load_settings(path).notify_channels == ("email",)  # legacy: email only
    path.write_text("[notify]\nemail_enabled = false\n")
    assert load_settings(path).notify_channels == ()
    path.write_text('[notify]\nchannels = []\nemail_enabled = true\nopenclaw_bin = "/opt/oc"\n')
    assert load_settings(path).notify_channels == () and load_settings(path).openclaw_bin == "/opt/oc"


def test_setup_helpers_write_notify_keys(tmp_path):
    assert parse_channels("telegram, EMAIL telegram") == ["telegram", "email"]
    assert parse_channels("none") == []
    with pytest.raises(ValueError, match="sms"):
        parse_channels("sms")
    path = tmp_path / "settings.toml"
    path.write_text('[owner]\ntimezone = "Europe/London"\n\n[notify]\nquiet_hours = "23:00-07:00"\n')
    status = save_notify_settings(
        {"channels": ["openclaw", "telegram"], "telegram_chat_id": "-100123", "email_enabled": False},
        path,
        EXAMPLE_PATH,
    )
    assert status == "updated settings.toml"
    s = load_settings(path)
    assert s.notify_channels == ("openclaw", "telegram") and s.telegram_chat_id == "-100123"
    assert s.quiet_hours == "23:00-07:00" and s.owner_timezone == "Europe/London" and not s.email_enabled
    assert "Where alerts and digests go" in path.read_text()  # the example's comments are kept
    assert save_notify_settings({"telegram_chat_id": "-100123"}, path, EXAMPLE_PATH) == "settings.toml unchanged"


def test_readiness_never_reads_out_secrets(monkeypatch):
    monkeypatch.setattr("fbm.credentials.get", lambda name: "SECRET-VALUE" if name == "telegram_bot_token" else None)
    monkeypatch.setattr(dispatch.openclaw, "resolve", lambda b: ["/usr/local/bin/openclaw"])
    s = Settings(notify_channels=("telegram", "openclaw", "email"), telegram_chat_id="1")
    ready = dispatch.readiness(s)
    assert ready["telegram"] == (True, "bot token stored, chat id set")
    assert ready["openclaw"] == (True, "CLI found (/usr/local/bin/openclaw)")
    assert ready["email"] == (False, "Gmail not configured")
    assert "SECRET" not in str(ready)
    assert dispatch.readiness(Settings(notify_channels=("telegram",)))["telegram"] == (False, "missing chat id")


def test_health_reports_chat_channels():
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    rows = health.check_notifications(
        False,
        [],
        now,
        ("openclaw", "telegram"),
        {"openclaw": (False, "OpenClaw CLI 'openclaw' not found"), "telegram": (True, "bot token stored, chat id set")},
    )
    by = {r["check"]: r for r in rows}
    assert [r["check"] for r in rows] == ["Email", "Telegram", "OpenClaw", "Outbox failures"]
    assert by["Telegram"]["status"] == "OK" and by["OpenClaw"]["status"] == "WARN"
    assert "fall back to direct Telegram" in by["OpenClaw"]["fix"]
    email = health.check_notifications(True, [], now, ("email",), {"email": (False, "Gmail not configured")})[0]
    assert email["status"] == "WARN" and "Gmail not configured" in email["detail"]


def test_send_email_and_notify_tools_route_through_the_dispatcher(monkeypatch):
    from fbm import tools

    seen = []
    monkeypatch.setattr(tools.notify_dispatch, "send", lambda *a, **k: seen.append(a) or {"status": "sent"})
    assert tools.call("send_email", {"subject": "S", "markdown_body": "B", "kind": "approval"}) == {"status": "sent"}
    assert tools.call("notify", {"subject": "S2", "markdown_body": "B2"}) == {"status": "sent"}
    assert seen == [("S", "B", "approval"), ("S2", "B2", "report")]
    assert tools.TOOLS["notify"].write and tools.TOOLS["send_email"].write
