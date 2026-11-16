"""`fbm digest`: section collection (each isolated), deterministic rendering from engine outputs, size budget, CLI."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from fbm.cli import app
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer
from fbm.notify import digest
from fbm.notify.digest import Unavailable
from fbm.settings import Settings

NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)  # 09:00 ET, Monday
TODAY = date(2026, 11, 16)
SETTINGS = Settings(owner_timezone="America/Chicago")


def _odds():
    cats = [("PTS", 0.81), ("REB", 0.47), ("AST", 0.78), ("STL", 0.61), ("BLK", 0.36), ("3PM", 0.89)]
    cats += [("FG%", 0.30), ("FT%", 0.55), ("TO", 0.19)]
    return {
        "opponent": "Euro Steppers",
        "window_et": "2026-11-16..2026-11-22",
        "categories": [{"cat": c, "p_win": p} for c, p in cats],
        "expected_cats_won": 4.91,
        "p_win_majority": 0.63,
        "games_started": {"me": 43, "opp": 45},
    }


def _data(**over):
    d = {
        "kind": "daily",
        "now": NOW,
        "today_et": TODAY,
        "overview": {
            "my_team": "My Team",
            "this_week_opponent": "Euro Steppers",
            "fantasy_week": {"number": 5, "start": "2026-11-16", "end": "2026-11-22"},
            "my_standing": {"rank": 3, "wins": 30, "losses": 20, "ties": 4},
            "source": "yahoo",
        },
        "odds": _odds(),
        "lineup": {
            "date_et": "2026-11-16",
            "changes": ["Ty Jerome: BN → IL", "Dereck Lively II: BN → UTIL"],
            "il_moves_needing_a_roster_move": [],
        },
        "first_tip": datetime(2026, 11, 17, 0, 0, tzinfo=UTC),
        "injuries": {
            "opponent": "Euro Steppers",
            "mine": [
                {"name": "Ty Jerome", "status": "Out", "injury_type": "Hip", "return_date": "2026-11-18", "slot": "IL"}
            ],
            "opp": [{"name": "Ja Morant", "status": "Out", "injury_type": "Hamstring", "return_date": None}],
        },
        "stream": {
            "weekly_add_limit": 4,
            "adds_available": 3,
            "moves": [
                {
                    "add": "Toumani Camara",
                    "drop": "Darius Acuff Jr.",
                    "add_games": 4,
                    "effective_et": "2026-11-16",
                    "gain_expected_cats": 0.281,
                    "via_waivers": False,
                }
            ],
            "baseline": {"expected_cats": 4.9, "p_win_majority": 0.63},
            "after_plan": {"expected_cats": 5.2, "p_win_majority": 0.7},
        },
        "radar": {
            "players": [
                {
                    "player": "Bennett Stirtz",
                    "id": "1",
                    "nba": "OKC",
                    "confidence": "medium",
                    "reasons": ["opportunity (high): Cason Wallace is out"],
                    "beats_worst_rostered": True,
                    "suggested_drop": {"player": "X", "id": "2"},
                }
            ]
        },
        "waiver": {"player": "Bennett Stirtz", "drop": "X", "action": "claim", "reason": "gain beats the bar"},
        "signals": {"signals": [{"player": "Danny Wolf", "nba": "BKN", "kind": "role_change", "note": "started 4"}]},
        "proposals": {
            "proposals": [
                {"ref": "P-7", "status": "pending_approval", "title": "Add Camara"},
                {"ref": "P-6", "status": "pending_review", "title": "In review"},
            ]
        },
        "offers": {"incoming_trades": [], "outgoing_trades": [], "waiver_claims": []},
        "health": {"checks": [{"check": "Data freshness", "status": "WARN", "detail": "injuries 30h old"}]},
    }
    d.update(over)
    return d


def test_daily_digest_is_verdict_first_with_engine_numbers():
    text = digest.render(_data(), SETTINGS)
    lines = text.splitlines()
    assert lines[0] == "**FBM daily · Mon Nov 16**"
    assert lines[1] == "My Team vs Euro Steppers · week 5 · 2026-11-16 to 2026-11-22 (ET) · #3 (30-20-4)"
    assert lines[2] == (
        "**Verdict:** Favored vs Euro Steppers: **63%** to win the week (4.9 of 9 cats expected). "
        "Today: 2 lineup change(s), stream Toumani Camara (+0.28 cats), 1 proposal(s) need you."
    )
    assert "**Lineup today** (first tip 6:00 PM CST)" in text  # owner's time zone
    assert "- Ty Jerome: BN → IL" in text
    assert "- Ahead: 3PM 89%, PTS 81%, AST 78%, STL 61%" in text and "- Behind: BLK 36%, FG% 30%, TO 19%" in text
    assert "- Games left this week: you 43 · them 45" in text
    assert "**Ty Jerome** Out (Hip, back ~2026-11-18) · on IL" in text and "- Euro Steppers: **Ja Morant** Out" in text
    assert "**Best stream** (adds left this week: 3)" in text and "**+0.28** expected cats" in text
    assert "- **Bennett Stirtz** (drop X): CLAIM: gain beats the bar" in text
    assert "P-7 Add Camara: needs your decision" in text and "P-6" not in text
    assert "Danny Wolf" in text and "- WARN Data freshness: injuries 30h old" in text
    assert len(text) <= digest.DIGEST_LIMIT


def test_unavailable_sections_get_a_short_note():
    d = _data(odds=Unavailable("GMError: no games"), stream=Unavailable("RuntimeError: boom"), waiver=None)
    text = digest.render(d, SETTINGS)
    assert "Matchup odds unavailable." in text
    assert "**Matchup**: unavailable (GMError: no games)" in text
    assert "**Best stream**: unavailable (RuntimeError: boom)" in text
    assert "**Lineup today**" in text and "Top waiver target" not in text


def test_quiet_day_and_demo_header():
    d = _data(
        lineup={"date_et": "2026-11-16", "changes": [], "il_moves_needing_a_roster_move": ["Z: roster full"]},
        stream={"weekly_add_limit": None, "moves": []},
        proposals={"proposals": []},
        overview={"my_team": "Me", "this_week_opponent": None, "fantasy_week": {}, "source": "demo"},
        first_tip=None,
    )
    text = digest.render(d, SETTINGS)
    assert "Me vs Euro Steppers · demo league" in text  # opponent from the odds when the league has none
    assert "No moves needed today." in text
    assert "- Already set: nothing to change." in text and "- Needs a roster move (proposal): Z: roster full" in text
    assert "- No add/drop beats standing pat." in text


def test_weekly_preview():
    stream = _data()["stream"]
    stream["moves"] = stream["moves"] * 2
    d = _data(kind="weekly", stream=stream)
    del d["lineup"], d["first_tip"], d["waiver"]
    text = digest.render(d, SETTINGS)
    assert text.startswith("**FBM weekly preview · Mon Nov 16**")
    assert "This week: 2 stream(s) (+0.56 cats), 1 proposal(s) need you." in text
    assert "- 3PM 89% · PTS 81% · AST 78% · STL 61% · FT% 55% · REB 47% · BLK 36% · FG% 30% · TO 19%" in text
    assert "**Streaming plan** (adds left this week: 3)" in text
    assert "- Expected cats 4.9 → 5.2; win the week 63% → 70%" in text
    assert "Lineup today" not in text


def test_digest_fits_the_budget_by_dropping_low_priority_sections():
    d = _data(health={"checks": [{"check": f"c{i}", "status": "FAIL", "detail": "x" * 90} for i in range(3)]})
    d["injuries"]["mine"] = d["injuries"]["mine"] * 4
    text = digest.render(d, SETTINGS, limit=900)
    assert len(text) <= 900 and "more section(s) cut to fit" in text
    assert text.splitlines()[2].startswith("**Verdict:**") and "**Lineup today**" in text


def test_untrusted_text_is_neutralized():
    d = _data()
    d["injuries"]["opp"][0]["injury_type"] = "[click](https://evil.example) **x** `y`"
    text = digest.render(d, SETTINGS)
    assert "](https" not in text and "(click)(https://evil.example) x 'y'" in text


def _league():
    me = FantasyTeam("t1", "Me", players=[RosterPlayer("a", "A", "BOS", ("PG", "UTIL"), "PG")])
    them = FantasyTeam("t2", "Them", players=[RosterPlayer("b", "B", "LAL", ("C", "UTIL"), "C")])
    return LeagueState("l", "L", 2027, "t1", [me, them], source="demo")


def _fake_svc(**over):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE games (home, away, start_utc, state, et_date, season_type)")
    conn.execute("INSERT INTO games VALUES ('BOS', 'NYK', '2026-11-17T00:30:00+00:00', 'pre', '2026-11-16', 2)")
    league = _league()
    calls = []

    def rec(name, value):
        def fn(*a, **k):
            calls.append((name, a, k))
            if isinstance(value, Exception):
                raise value
            return value

        return fn

    fns = {
        "league_overview": {"my_team": "Me", "this_week_opponent": "Them", "fantasy_week": {}, "source": "demo"},
        "simulate_matchup_tool": _odds(),
        "lineup_steps": {"date_et": "2026-11-16", "changes": []},
        "injuries": {"injuries": [{"espn_id": "b", "name": "B", "status": "Out"}]},
        "streaming_plan": RuntimeError("sim exploded"),
        "fa_radar_tool": {"players": []},
        "waiver_decision": {},
        "signals_tool": {"signals": []},
        "list_proposals": {"proposals": []},
        "pending_offers_tool": {"available": False, "message": "demo"},
        "health_check": {"error": "HT_UNHEALTHY", "message": "Hashtag is stale"},
    }
    fns.update(over)
    svc = SimpleNamespace(**{k: rec(k, v) for k, v in fns.items()})
    svc.active_league = lambda: league
    svc._default_opponent = lambda lg: lg.team("t2")
    svc._conn = lambda: conn
    return svc, calls


def test_collect_isolates_each_section_and_uses_the_trigger():
    svc, calls = _fake_svc()
    d = digest.collect("daily", record=True, now=NOW, svc=svc)
    assert d["stream"] == Unavailable("RuntimeError: sim exploded")
    assert d["health"] == Unavailable("Hashtag is stale")
    assert d["first_tip"] == datetime(2026, 11, 17, 0, 30, tzinfo=UTC)
    assert d["injuries"]["opp"][0]["name"] == "B" and d["injuries"]["mine"] == []
    assert d["waiver"] is None  # no radar target: no waiver call
    triggers = {name: k.get("trigger") for name, _a, k in calls if "trigger" in k}
    assert triggers == {"lineup_steps": "daily-gm", "streaming_plan": "daily-gm"}
    text = digest.render(d, SETTINGS)
    assert "**Best stream**: unavailable (RuntimeError: sim exploded)" in text and "**Matchup vs Euro" in text

    weekly = digest.collect("weekly", record=False, now=NOW, svc=svc)
    assert "lineup" not in weekly and "waiver" not in weekly
    assert calls[-1][0] == "health_check" and ("streaming_plan", (), {"trigger": "owner-ask"}) in calls


def test_top_target_asks_waiver_decision():
    radar = {"players": [{"id": "9", "beats_worst_rostered": True, "suggested_drop": {"id": "2"}}]}
    svc, calls = _fake_svc(fa_radar_tool=radar, waiver_decision={"player": "P", "action": "wait"})
    d = digest.collect("daily", now=NOW, svc=svc)
    assert d["waiver"] == {"player": "P", "action": "wait"} and ("waiver_decision", ("9", "2"), {}) in calls


def test_cli_digest_prints_and_sends(monkeypatch):
    from fbm.notify import dispatch
    from fbm.notify.base import NotifyError

    monkeypatch.setattr(digest, "build", lambda kind, record, now: f"**FBM {kind}** record={record}")
    sent = []
    monkeypatch.setattr(dispatch, "send", lambda *a, **k: sent.append((a, k)) or {"status": "sent"})
    runner = CliRunner()
    out = runner.invoke(app, ["digest"])
    assert out.exit_code == 0 and out.stdout == "**FBM daily** record=False\n" and sent == []
    out = runner.invoke(app, ["digest", "--kind", "weekly", "--send"])
    assert out.exit_code == 0 and "record=True" in out.stdout
    (subject, body, kind), kw = sent[0]
    assert subject.startswith("Weekly preview · ") and kind == "report" and kw == {"title": False}

    def fail(*a, **k):
        raise NotifyError("Telegram rejected the bot token")

    monkeypatch.setattr(dispatch, "send", fail)
    out = runner.invoke(app, ["digest", "--send"])
    assert out.exit_code == 1 and "Telegram rejected the bot token" in out.stderr
    assert runner.invoke(app, ["digest", "--kind", "monthly"]).exit_code == 1


def test_cli_digest_failure_is_a_short_stderr_message(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(digest, "build", boom)
    out = CliRunner().invoke(app, ["digest"])
    assert out.exit_code == 1 and out.stdout == "" and "RuntimeError: database is locked" in out.stderr


def test_build_rejects_unknown_kinds():
    with pytest.raises(ValueError, match="daily, weekly"):
        digest.build("hourly")
