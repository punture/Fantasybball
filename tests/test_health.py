from datetime import UTC, date, datetime, timedelta

import pytest

from fbm.gm import health
from fbm.store import db

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _by(checks, name):
    return next(c for c in checks if c["check"] == name)


def test_season_phase():
    dates = ["2026-10-20", "2027-04-12"]
    assert health.season_phase(dates, date(2026, 10, 16)) == "preseason"
    assert health.season_phase(dates, date(2026, 11, 1)) == "regular"
    assert health.season_phase(dates, date(2027, 6, 1)) == "offseason"
    assert health.season_phase([], date(2026, 11, 1)) == "offseason"


def test_data_freshness_thresholds():
    runs = {
        "schedule:2027": _iso(NOW - timedelta(hours=5)),
        "boxscores:2027": _iso(NOW - timedelta(hours=20)),
        "injuries": _iso(NOW - timedelta(hours=50)),
    }
    out = health.check_data_freshness(runs, "regular", games_last_day=8, now=NOW)
    assert _by(out, "ESPN schedule")["status"] == "OK"
    assert _by(out, "ESPN boxscores")["status"] == "WARN"
    assert _by(out, "ESPN injuries")["status"] == "FAIL"
    assert _by(out, "ESPN news")["status"] == "FAIL"
    relaxed = health.check_data_freshness(runs, "preseason", games_last_day=0, now=NOW)
    assert _by(relaxed, "ESPN injuries")["status"] == "OK"


def test_yahoo_demo_is_warn_not_fail():
    out = health.check_yahoo("demo", {"yahoo_client_id": False}, False, True, None, "", 0)
    assert {c["status"] for c in out} == {"WARN", "OK"}
    assert "approval pending" in _by(out, "League source")["detail"]
    missing_demo = health.check_yahoo("demo", {}, True, False, None, "", 0)
    assert _by(missing_demo, "Demo league")["status"] == "FAIL"


def test_yahoo_live_checks():
    creds = {"a": True, "b": True}
    out = health.check_yahoo("yahoo", creds, True, False, 30.0, "", 0)
    assert health.overall(out) == "OK"
    bad = health.check_yahoo("yahoo", {"a": False}, True, False, 90.0, "401 token expired", 3)
    assert _by(bad, "Yahoo credentials")["status"] == "FAIL"
    assert _by(bad, "Yahoo snapshot")["status"] == "WARN"
    assert _by(bad, "Yahoo refresh")["status"] == "FAIL"
    assert "3 unmatched" in _by(bad, "Yahoo ID matching")["detail"]


def test_yahoo_web_session_replaces_api_credentials():
    out = health.check_yahoo("yahoo", {"a": False}, True, False, 30.0, "", 0, web_session_age_days=2.0)
    assert health.overall(out) == "OK"
    assert "web session" in _by(out, "Yahoo access")["detail"]
    assert not any(c["check"] == "Yahoo credentials" for c in out)
    demo = health.check_yahoo("demo", {"a": False}, True, True, None, "", 0, web_session_age_days=0.0)
    assert "web session ready" in _by(demo, "League source")["detail"]


def test_proposals():
    future, past = _iso(NOW + timedelta(hours=5)), _iso(NOW - timedelta(hours=1))
    rows = [
        {
            "ref": "P-1",
            "status": "awaiting_owner",
            "created_at": _iso(NOW - timedelta(hours=30)),
            "expires_at": future,
            "decided_at": None,
            "executed_at": _iso(NOW - timedelta(hours=20)),
        },
        {
            "ref": "P-2",
            "status": "approved",
            "created_at": _iso(NOW - timedelta(hours=5)),
            "expires_at": future,
            "decided_at": _iso(NOW - timedelta(hours=4)),
            "executed_at": None,
        },
        {
            "ref": "P-3",
            "status": "pending_review",
            "created_at": _iso(NOW - timedelta(hours=20)),
            "expires_at": past,
            "decided_at": None,
            "executed_at": None,
        },
    ]
    out = health.check_proposals(rows, NOW)
    assert [c["status"] for c in out] == ["WARN", "WARN", "WARN"]
    assert "P-3" in out[2]["detail"]
    assert health.overall(health.check_proposals([], NOW)) == "OK"


def test_journal_gaps():
    today = date(2026, 11, 5)
    game_days = {date(2026, 11, 3), date(2026, 11, 4)}
    entry = "\n## 09:00 local / Wed 20:00 ET · decision · Daily GM\n"
    lineup = "\n## 16:16 local / Wed 04:16 ET · execution · Lineup 2026-11-04\n"
    journal = {date(2026, 11, 2): entry, date(2026, 11, 3): entry, date(2026, 11, 4): entry + lineup}
    r = health.check_journal(journal, "regular", today, game_days, date(2026, 10, 20))
    assert r["status"] == "WARN" and "2026-11-03" in r["detail"] and "lineup" in r["detail"]
    r = health.check_journal({date(2026, 11, 4): entry}, "regular", today, set(), date(2026, 10, 20))
    assert r["status"] == "FAIL"
    assert health.check_journal({}, "preseason", today, set(), None)["status"] == "OK"


def test_journal_new_install_is_not_a_failure():
    today = date(2026, 11, 16)
    entry = "\n## 09:00 local / Mon 09:00 ET · decision · Daily GM\n"
    # Installed today: nothing before the install is expected.
    r = health.check_journal({today: entry}, "regular", today, {date(2026, 11, 15)}, date(2026, 10, 20), today)
    assert r["status"] == "OK" and "new install" in r["detail"]
    # Installed 3 days ago, every full day since has its entries: OK; a missed day after install still counts.
    first = date(2026, 11, 13)
    full = {date(2026, 11, d): entry for d in (13, 14, 15)}
    assert health.check_journal(full, "regular", today, set(), date(2026, 10, 20), first)["status"] == "OK"
    gap = {date(2026, 11, 13): entry, date(2026, 11, 15): entry}
    r = health.check_journal(gap, "regular", today, set(), date(2026, 10, 20), first)
    assert r["status"] in ("WARN", "FAIL") and "2026-11-14" in r["detail"] and "2026-11-12" not in r["detail"]


def test_notifications_and_storage():
    outbox = [{"status": "failed", "subject": "Daily", "created_at": _iso(NOW - timedelta(hours=2))}]
    out = health.check_notifications(False, outbox, NOW)
    assert out[0]["status"] == "OK" and "disabled" in out[0]["detail"]
    assert out[1]["status"] == "WARN"
    assert health.check_storage(True, 5_000_000)[0]["status"] == "OK"
    assert health.check_storage(True, None)[0]["status"] == "FAIL"
    assert health.check_storage(False, None)[0]["status"] == "FAIL"


def test_gather_with_temp_db(tmp_path, monkeypatch):
    from fbm.gm import memory

    path = tmp_path / "fbm.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.execute("INSERT INTO sync_log VALUES ('news', ?, 'x')", (_iso(NOW - timedelta(hours=1)),))
    conn.execute(
        "INSERT INTO games VALUES ('1', 2027, 2, ?, '2026-10-20', 'BOS', 'NYK', 'pre', 0, 0, 1, 0, ?)",
        (_iso(NOW), _iso(NOW)),
    )
    conn.commit()
    monkeypatch.setattr(memory, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(health, "_cred_presence", lambda: {"yahoo_client_id": True})
    monkeypatch.setattr(health, "_web_session_age_days", lambda now: None)
    r = health.gather(conn=conn, db_path=path, now=datetime(2026, 10, 16, 10, tzinfo=UTC))
    conn.close()
    assert r["phase"] == "preseason"
    assert _by(r["checks"], "ESPN news")["status"] == "OK"
    assert _by(r["checks"], "Scheduled runs")["status"] == "OK"
    assert r["overall"] in ("WARN", "FAIL")


@pytest.mark.parametrize("statuses,expected", [(["OK"], "OK"), (["OK", "WARN"], "WARN"), (["WARN", "FAIL"], "FAIL")])
def test_overall(statuses, expected):
    assert health.overall([{"status": s} for s in statuses]) == expected


# ------------------------------------------------------------------ OS-scheduled jobs on the computer's clock

UTC_NOV, LA_NOV = 300, -180  # minutes ahead of ET in November: a UTC box, an owner in Los Angeles


def test_schedule_check_catches_entries_on_the_wrong_clock():
    assert health.check_schedule("none", {}, UTC_NOV, LA_NOV) == []
    assert health.check_schedule("agent-app", {}, UTC_NOV, LA_NOV) == []
    # Installed by an older FBM in the owner's zone on a UTC box: lineup alerts miss every tip-off.
    [old] = health.check_schedule("engine-only", {}, UTC_NOV, LA_NOV)
    assert old["status"] == "FAIL" and "8h" in old["detail"] and "fbm automation install" in old["fix"]
    [same] = health.check_schedule("engine-only", {}, LA_NOV, LA_NOV)
    assert same["status"] == "WARN" and "no install record" in same["detail"]
    # Installed in July (EDT) on the UTC box, checked in November (EST): an hour off until re-installed.
    summer = {"lineup-guard": {"zone": "UTC", "offset_min": 240}, "health": {"zone": "UTC", "offset_min": 300}}
    [drift] = health.check_schedule("scheduler", summer, UTC_NOV, LA_NOV)
    assert drift["status"] == "WARN" and drift["detail"].startswith("lineup-guard:") and "1h off" in drift["detail"]
    [ok] = health.check_schedule("engine-only", {"health": {"zone": "UTC", "offset_min": 300}}, UTC_NOV, LA_NOV)
    assert ok["status"] == "OK" and "UTC" in ok["detail"]


def test_schedule_checks_read_the_computers_zone_and_never_crash(monkeypatch):
    from fbm import automation

    monkeypatch.setattr(automation, "scheduler_timezone", lambda: "Etc/UTC")
    monkeypatch.setattr(automation, "read_install_record", lambda: {})
    day = date(2026, 11, 16)
    assert health._schedule_checks("none", "America/Los_Angeles", day) == []
    [row] = health._schedule_checks("engine-only", "America/Los_Angeles", day)
    assert row["status"] == "FAIL"
    [bad] = health._schedule_checks("engine-only", "Not/AZone", day)
    assert bad["status"] == "WARN" and "not checked" in bad["detail"]


def test_fail_alert_lists_only_the_fail_rows():
    checks = [
        health.result("Yahoo access", "FAIL", "Flaim route, not signed in", "`fbm flaim login`"),
        health.result("ESPN news", "WARN", "30h old"),
        health.result("Storage", "FAIL", "data/fbm.db missing"),
    ]
    subject, body, names = health.fail_alert(checks)
    assert subject == "FBM health: 2 check(s) FAIL" and names == ["Storage", "Yahoo access"]
    assert "- Yahoo access: Flaim route, not signed in Fix: `fbm flaim login`" in body
    assert "- Storage: data/fbm.db missing\n" in body and "ESPN news" not in body


def test_health_notify_on_fail_messages_once_a_day(monkeypatch, tmp_path):
    """The engine-only health job (`fbm health --notify-on-fail`) must tell the owner about a FAIL (e.g. a lapsed
    Flaim sign-in) instead of only writing a log nobody reads; the same failures are messaged once a day."""
    from typer.testing import CliRunner

    from fbm.cli import app
    from fbm.gm import services as svc
    from fbm.notify import dispatch

    conn = db.connect(tmp_path / "fbm.db")
    report = {"overall": "FAIL", "phase": "regular", "checks": [health.result("Yahoo access", "FAIL", "lapsed")]}
    sent = []
    monkeypatch.setattr(svc, "health_check", lambda: report)
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(
        dispatch, "send", lambda subject, body, kind, **k: sent.append((subject, body, kind)) or {"status": "sent"}
    )
    runner = CliRunner()
    assert runner.invoke(app, ["health"]).exit_code == 1 and sent == []  # plain `fbm health` never messages
    r = runner.invoke(app, ["health", "--notify-on-fail"])
    assert r.exit_code == 1 and "Health alert: sent" in r.output
    assert sent[0][0] == "FBM health: 1 check(s) FAIL" and "Yahoo access: lapsed" in sent[0][1]
    assert sent[0][2] == "report"  # silent and held in quiet hours: not a 3 a.m. ring
    assert runner.invoke(app, ["health", "--notify-on-fail"]).exit_code == 1 and len(sent) == 1  # once a day
    report["checks"].append(health.result("Storage", "FAIL", "data/fbm.db missing"))
    assert runner.invoke(app, ["health", "--notify-on-fail"]).exit_code == 1 and len(sent) == 2  # new failure

    def down(*a, **k):
        raise RuntimeError("telegram down")

    report["checks"].append(health.result("ESPN news", "FAIL", "stale"))
    monkeypatch.setattr(dispatch, "send", down)
    r = runner.invoke(app, ["health", "--notify-on-fail"])
    assert r.exit_code == 1 and "Health alert not sent: RuntimeError" in r.output  # still exit 1, no traceback
    conn.close()
