"""protect_plan end to end (services -> tool -> CLI) on a tiny offline league: ET days, banked stats, tip-off locks."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from typer.testing import CliRunner

from fbm import tools
from fbm.analytics.categories import NINE_CAT
from fbm.analytics.simulation import PlayerModel
from fbm.cli import app
from fbm.gm import services as svc
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer, SlotSpec
from fbm.store import db
from tests.test_protect import BIG, GUARD, ME_BANKED, OPP, OPP_BANKED

MON, THU, SUN = date(2026, 11, 16), date(2026, 11, 19), date(2026, 11, 22)
ME, OPP_KEY = "466.l.1.t.1", "466.l.1.t.2"
FUTURE = "2099-01-01T00:00:00+00:00"  # tip-off far ahead: only `state` marks a game as started


def _games_conn():
    conn = db.connect(":memory:")
    rows = []
    for i, day in enumerate(pd.date_range(MON, SUN).date):
        for home, away in (("AAA", "OOO"), ("BBB", "PPP")):
            started = day < THU or (day == THU and home == "AAA")  # Thursday: Big's game has tipped off
            rows.append(
                (f"{i}{home}", 2027, 2, FUTURE, day.isoformat(), home, away, "in" if started else "pre", 0, 0, 1, "x")
            )
    conn.executemany(
        "INSERT INTO games(event_id, season, season_type, start_utc, et_date, home, away, state, completed, neutral, "
        "time_valid, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return conn


def _league() -> LeagueState:
    mine = FantasyTeam(
        ME,
        "My Team",
        players=[
            RosterPlayer("big", "Big Man", "AAA", ("C", "UTIL"), "C"),
            RosterPlayer("guard", "Guard", "BBB", ("PG", "UTIL"), "UTIL"),
        ],
    )
    theirs = FantasyTeam(
        OPP_KEY,
        "Rivals",
        players=[
            RosterPlayer("o1", "Opp One", "OOO", ("C", "UTIL"), "C"),
            RosterPlayer("o2", "Opp Two", "PPP", ("PG", "UTIL"), "UTIL"),
        ],
    )
    return LeagueState(
        "466.l.1",
        "Test",
        2027,
        ME,
        [mine, theirs],
        slots=(SlotSpec("C", 1, True), SlotSpec("UTIL", 1, True), SlotSpec("BN", 2, False)),
        categories=NINE_CAT,
        week=5,
        week_start=MON,
        week_end=SUN,
        opponent_key=OPP_KEY,
        banked={ME: dict(ME_BANKED), OPP_KEY: dict(OPP_BANKED)},
        fetched_at="2026-11-20T01:00:00+00:00",  # Thu 20:00 ET
    )


@pytest.fixture()
def league(monkeypatch):
    lg, conn = _league(), _games_conn()
    samples = {"big": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    fake_ctx = SimpleNamespace(
        hashtag=False,
        models=lambda ids, conn=None, **kw: {pid: PlayerModel(pid, samples[pid]) for pid in ids},
        return_dates=lambda conn=None: {},
    )
    monkeypatch.setattr(svc, "active_league", lambda: lg)
    monkeypatch.setattr(svc, "ctx", lambda: fake_ctx)
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc, "_value", lambda c, punts: pd.Series({"big": 1.0, "guard": 2.0, "o1": 1.0, "o2": 1.0}))
    monkeypatch.setattr(svc, "_strategy_punts", lambda punts: ())
    monkeypatch.setattr(svc, "_scoring_metric", lambda: "expected_cats")
    monkeypatch.setattr(svc, "nba_today", lambda: THU)
    monkeypatch.setattr(svc, "_week_window", lambda day=None: (day or THU, MON, SUN))
    return lg


def test_protect_plan_benches_the_big_from_friday_with_exact_steps(league):
    out = svc.protect_plan(n=1500)
    assert out["window_et"] == "2026-11-19..2026-11-22" and out["actionable_from_et"] == "2026-11-19"
    assert out["late_week"] is True and out["objective"] == "expected_cats" and out["banked_this_week"] is True
    assert out["locked_game_started"] == ["Big Man"]  # his Thursday game is under way: banked, not benchable
    best = out["actions"][0]
    assert best["kind"] == "sit" and best["players"] == ["Big Man"]
    assert best["days_et"] == {"Big Man": ["2026-11-20", "2026-11-21", "2026-11-22"]}
    assert best["steps"] == ["In Yahoo, for Fri 11-20, Sat 11-21, Sun 11-22 (ET): move Big Man from C to BN."]
    assert best["gain_expected_cats"] >= out["min_gain"] and best["games_lost"] == 3
    before, after = best["win_odds"]["FT%"]
    assert after > before and out["baseline"]["win_odds"]["FT%"] == before
    assert out["recommendation"].startswith("Bench: benching Big Man for the rest of the week (Fri, Sat, Sun)")
    assert out["protect_mode"] is not None and "steps" not in out["protect_mode"]
    assert any("never changes your league" in n for n in out["notes"])


def test_a_later_et_day_only_benches_from_that_day(league):
    out = svc.protect_plan(day="2026-11-21", n=1500)
    assert out["window_et"] == "2026-11-19..2026-11-22" and out["actionable_from_et"] == "2026-11-21"
    best = out["actions"][0]
    assert best["days_et"] == {"Big Man": ["2026-11-21", "2026-11-22"]}
    assert best["steps"] == ["In Yahoo, for Sat 11-21, Sun 11-22 (ET): move Big Man from C to BN."]


def test_one_win_per_week_league_reports_p_win_objective(league, monkeypatch):
    monkeypatch.setattr(svc, "_scoring_metric", lambda: "p_win_week")
    out = svc.protect_plan(n=1500)
    assert out["objective"] == "p_win_week" and out["min_gain"] == pytest.approx(0.02)
    assert out["actions"] == []  # I win the week either way
    assert "doesn't move P(win week)" in out["recommendation"]


def test_protect_plan_tool_and_cli(league):
    out = tools.call("protect_plan", {"day": "2026-11-19", "top": "1"})
    assert len(out["actions"]) == 1 and out["actions"][0]["players"] == ["Big Man"]
    past = tools.call("protect_plan", {"day": "2026-11-18"})
    assert "error" in past and "in the past" in past["error"]
    assert "protect_plan(day=None, opponent=None, top=3)" == tools.TOOLS["protect_plan"].signature()
    res = CliRunner().invoke(app, ["protect"])
    assert res.exit_code == 0, res.output
    assert "Bench: benching Big Man" in res.output and "move Big Man from C to BN" in res.output
    bad = CliRunner().invoke(app, ["protect", "--day", "2026-11-18"])
    assert bad.exit_code == 1 and "in the past" in bad.output
