"""Services wiring of the league rules: Most Categories objective, Daily - Tomorrow deadline, tipped games and IL
returners in streaming_plan, the lineup tools and evaluate_add_drop (fake analytics context, no network)."""

import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from fbm.analytics.categories import NINE_CAT
from fbm.analytics.simulation import COL, STATS, PlayerModel, play_probability
from fbm.gm import choosers, memory, policy
from fbm.gm import services as svc
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer, SlotSpec
from fbm.store import db

D1, D2, D3 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
SLOTS = (SlotSpec("PG", 1, True), SlotSpec("UTIL", 1, True), SlotSpec("BN", 1, False), SlotSpec("IL", 1, False))
SCHEDULE = {
    D1: ["AAA", "BBB", "DDD", "OOO", "FFF", "III"],
    D2: ["AAA", "BBB", "III", "OOO", "FFF"],
    D3: ["III", "OOO", "FFF", "GGG"],
}
TEAMS = {"A": "AAA", "B": "BBB", "D": "DDD", "I": "III", "O1": "OOO", "F": "FFF", "G": "GGG"}
HEADONE_TOMORROW = {"settings": {"scoring_type": "headone", "weekly_deadline": "tomorrow"}}
HEAD_TODAY = {"settings": {"scoring_type": "head", "weekly_deadline": "intraday"}}


def line(**kw) -> np.ndarray:
    row = np.zeros(len(STATS))
    for k, v in kw.items():
        row[COL[k]] = v
    return row


GOOD = line(pts=20, reb=8, ast=5, stl=1, blk=1, tov=2, tpm=2, fgm=8, fga=16, ftm=2, fta=3)
MEH = line(pts=8, reb=3, ast=2, stl=0, blk=0, tov=1, tpm=1, fgm=3, fga=8, ftm=1, fta=2)
SAMPLES = {"A": GOOD, "B": MEH, "D": MEH, "I": GOOD, "O1": GOOD, "F": GOOD, "G": MEH}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def player(pid, selected, status="", eligible=("PG", "UTIL")):
    return RosterPlayer(pid, pid, TEAMS[pid], eligible, selected, status)


def world(monkeypatch, conn, tmp_path, summary, tipped=frozenset()):
    """My roster is full (A, B, D active; I on IL, Out, back on D2 per ESPN). Free agents F and G."""
    me = FantasyTeam(
        "t1",
        "Me",
        players=[
            player("A", "PG"),
            player("B", "UTIL"),
            player("D", "BN"),
            player("I", "IL", "O", ("PG", "UTIL", "IL", "IL+")),
        ],
    )
    opp = FantasyTeam("t2", "Opp", players=[player("O1", "PG")])
    league = LeagueState(
        "L", "Test", 2027, "t1", [me, opp], slots=SLOTS, source="demo", opponent_key="t2", max_weekly_adds=4
    )
    cats = [c.key for c in NINE_CAT]
    gs = {"A": 2.0, "B": -1.0, "D": -2.0, "I": 3.0, "O1": 2.0, "F": 1.0, "G": -0.5}
    values = pd.DataFrame({pid: {f"g_{k}": g / 9 for k in cats} for pid, g in gs.items()}).T
    values["g_total"] = pd.Series(gs)
    values["name"], values["team"], values["position"] = list(gs), [TEAMS[p] for p in gs], "G"
    projections = values[["name", "team"]].copy()
    statuses, back = {"I": "O"}, {"I": D2}

    class Ctx:
        cfg = SimpleNamespace(categories=NINE_CAT)
        ros_values = None
        hashtag = False
        ht_as_of = None
        teamless = frozenset()

        def __init__(self):
            self.values, self.projections = values, projections

        def statuses(self, c=None):
            return statuses

        def return_dates(self, c=None):
            return dict(back)

        def value_map(self, column="g_total"):
            return values[column].to_dict()

        def view_g(self, view=None, c=None):
            return values

        def models(self, ids, c=None, horizon="week", view=None):
            out = {}
            for pid in ids:
                prob = play_probability(statuses.get(pid, ""))
                ret = back.get(pid) if prob <= 0 else None
                out[pid] = PlayerModel(pid, SAMPLES[pid][None], prob, return_date=ret, return_play_prob=0.6)
            return out

    for day, teams in SCHEDULE.items():
        for t in teams:
            conn.execute(
                "INSERT OR REPLACE INTO games(event_id, season, season_type, start_utc, et_date, home, away, state, "
                "completed, neutral, time_valid, updated_at) VALUES (?,2027,2,?,?,?,'ZZZ','pre',0,0,1,'x')",
                (f"{t}{day}", f"{day}T23:00:00+00:00", day.isoformat(), t),
            )
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "ctx", lambda: Ctx())
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc, "_week_window", lambda day=None: (D1, D1, D3))
    monkeypatch.setattr(svc, "_strategy_punts", lambda punts=None: ())
    monkeypatch.setattr(svc, "_league_summary", lambda: summary)
    monkeypatch.setattr(svc, "nba_today", lambda: D1)
    monkeypatch.setattr(svc, "_tipped_now", lambda day: set(tipped) if day == D1 else set())
    monkeypatch.setattr(svc, "adds_used_this_week", lambda today=None: 0)
    monkeypatch.setattr(memory, "JOURNAL_DIR", tmp_path / "journal")
    return league


def logged_situation(conn, kind):
    row = conn.execute("SELECT situation FROM decisions WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return json.loads(row["situation"])


def test_streaming_plan_in_a_most_categories_daily_tomorrow_league(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEADONE_TOMORROW)
    out = svc.streaming_plan(n=200)
    assert out["objective"] == "p_win_week" and out["lineup_deadline"] == "tomorrow"
    assert out["first_change_et"] == D2.isoformat()
    assert all(m["effective_et"] >= D2.isoformat() for m in out["moves"])
    assert "p_win_week" in out["baseline"] and "p_win_week" in out["after_plan"]
    # Full roster: the IL player back on D2 needs a spot (a drop) — surfaced, not silently ignored.
    assert any(line.startswith("I: can return from IL on 2026-11-17") for line in out["il_returns_needing_a_spot"])
    situation = logged_situation(conn, "stream_plan")
    assert situation["objective"] == "p_win" and situation["p_win_per_cat"] == pytest.approx(0.25)
    assert any("Daily - Tomorrow" in n for n in out["notes"])


def test_streaming_plan_in_a_categories_league_searches_with_the_choosers_weights(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEAD_TODAY, tipped={"FFF"})
    out = svc.streaming_plan(n=200)
    assert out["objective"] == "expected_cats" and out["lineup_deadline"] == "today"
    assert out["first_change_et"] == D1.isoformat()
    situation = logged_situation(conn, "stream_plan")
    assert "objective" not in situation
    params = policy.all_values(conn)
    expected = choosers.week_weights(situation["odds"], params, situation["punts"])
    label = {c.key: c.label for c in NINE_CAT}
    assert out["category_weights"] == pytest.approx({label[k]: round(w, 3) for k, w in expected.items()}, abs=1e-3)
    for m in out["moves"]:  # F's game already tipped on D1: never credited to an add
        if m["add_id"] == "F" and m["effective_et"] == D1.isoformat():
            assert m["add_games"] == 2


def test_lineup_tools_in_a_daily_tomorrow_league(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEADONE_TOMORROW)
    nxt = svc.lineup_steps()
    assert nxt["date_et"] == D2.isoformat() and nxt["lineup_deadline"] == "tomorrow"
    assert nxt["first_changeable_et"] == D2.isoformat() and "lineup_locked_note" not in nxt
    assert any(x.startswith("I: can return from IL") for x in nxt["il_returns_needing_a_spot"])
    assert nxt["objective"] == "p_win_week"
    assert "cat_weights" not in logged_situation(conn, "lineup")  # this simulated week is decided: no pivotal cat
    today = svc.lineup_steps(day=D1.isoformat())
    assert today["changes"] == [] and "already locked" in today["lineup_locked_note"]
    assert today["locked_game_started"] == []  # locked by the deadline, not by tip-offs
    assert svc.optimize_lineup()["date_et"] == D2.isoformat()


def test_most_categories_lineup_searches_and_chooses_with_pivotal_weights(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEADONE_TOMORROW)
    pivotal = {c.key: (1.0 if c.key == "blk" else 0.25) for c in NINE_CAT}
    monkeypatch.setattr(svc, "_pivotal_weights", lambda league, opp, start, punts: pivotal)
    out = svc.optimize_lineup()
    assert out["swing_weights"]["BLK"] == 1.0 and out["swing_weights"]["PTS"] == 0.25
    situation = logged_situation(conn, "lineup")
    assert situation["objective"] == "p_win" and situation["cat_weights"] == pivotal


def test_lineup_tools_in_a_daily_today_league(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEAD_TODAY, tipped={"AAA"})
    out = svc.optimize_lineup()
    assert out["date_et"] == D1.isoformat() and out["lineup_deadline"] == "today"
    assert out["locked_game_started"] == ["A"] and out["objective"] == "expected_cats"


def test_evaluate_add_drop_follows_the_deadline_and_tipped_games(monkeypatch, conn, tmp_path):
    world(monkeypatch, conn, tmp_path, HEADONE_TOMORROW)
    out = svc.evaluate_add_drop(add="F", drop="D", n=300)
    assert out["effective_et"] == D2.isoformat() and out["lineup_deadline"] == "tomorrow"
    assert out["games_left_this_week"] == {"add": 2, "drop": 0}  # F from D2; D only plays D1 (kept that day)
    assert 0.0 <= out["p_win_week_after"] <= 1.0
    world(monkeypatch, conn, tmp_path, HEAD_TODAY, tipped={"FFF"})
    out = svc.evaluate_add_drop(add="F", drop="D", n=300)
    assert out["effective_et"] == D1.isoformat()
    assert out["games_left_this_week"] == {"add": 2, "drop": 1}  # F's D1 game already tipped


def test_daily_tomorrow_lineup_weighs_this_weeks_odds_banked_stats_included(monkeypatch, conn, tmp_path):
    # Tomorrow is the first changeable day: its swing weights come from this week's odds (banked stats, HT's odds in
    # HT mode, today's lineup as set), not from a fresh simulation starting tomorrow with nothing banked.
    world(monkeypatch, conn, tmp_path, HEADONE_TOMORROW)
    starts = []

    def fake_odds(league, opp, n, start_day=None):
        starts.append(start_day)
        return {c.key: 0.5 for c in NINE_CAT}, "fbm_sim"

    monkeypatch.setattr(svc, "_week_odds", fake_odds)
    svc.optimize_lineup()
    svc.optimize_lineup(day=D3.isoformat())  # a later day is simulated from that day
    assert starts == [None, D3]


def test_playoff_week_anchor_ignores_weeks_rolled_by_the_calendar(monkeypatch):
    from dataclasses import replace

    league = LeagueState("L", "T", 2027, "t1", [], week=7, week_start=D1, week_end=D3)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    monkeypatch.setattr(svc, "active_league", lambda: league)
    assert svc._week_anchor() == (7, D1, D3)
    league = replace(league, rolled_from_week=6)
    assert svc._week_anchor() is None


def test_opponent_streams_follow_the_same_deadline(monkeypatch, conn, tmp_path):
    from fbm.analytics import streaming

    seen = []
    real = streaming.plan_streams

    def spy(ctx, *args, **kwargs):
        seen.append(ctx)
        return real(ctx, *args, **kwargs)

    monkeypatch.setattr(streaming, "plan_streams", spy)
    for summary, tipped, start in ((HEADONE_TOMORROW, set(), D2), (HEAD_TODAY, {"FFF"}, D1)):
        league = world(monkeypatch, conn, tmp_path, summary, tipped=tipped)
        opp = league.team("t2")
        _, _, _, _, _, _, my_plans, _ = svc._matchup(league, league.my_team, opp, None, 100)
        assert svc._opponent_streaming(league, opp, my_plans, [D1, D2, D3], 2) is not None
        assert seen[-1].start == start and seen[-1].tipped == {D1: tipped}


def test_pct_risk_sits_only_shooters_below_the_impact_threshold(monkeypatch, conn, tmp_path):
    # policy.pct_risk.max_impact is a (negative) G impact: only players whose FG%/FT% impact is below it sit. The
    # option now really benches them (even with an open slot), so the threshold's sign matters.
    league = world(monkeypatch, conn, tmp_path, HEAD_TODAY)  # A (+0.22 G per cat), B (-0.11), D (-0.22): D1 games
    monkeypatch.setattr(svc, "_week_odds", lambda *a, **k: ({c.key: 0.5 for c in NINE_CAT}, "fbm_sim"))
    base = policy.all_values(conn)

    def starters(max_impact: float) -> dict[str, list[str]]:
        params = {**base, "policy.pct_risk.enabled": True, "policy.pct_risk.max_impact": max_impact}
        monkeypatch.setattr(svc._policy, "all_values", lambda c=None: params)
        svc._lineup_plan(league, D1, True, "owner-ask", True)
        row = conn.execute("SELECT options FROM decisions WHERE kind = 'lineup' ORDER BY id DESC LIMIT 1").fetchone()
        return {o["id"]: o["spec"]["starters"] for o in json.loads(row["options"])}

    out = starters(-0.15)
    assert out["weighted"] == ["A", "B"] and out["pct_risk"] == ["A", "B"]  # only D is below -0.15 (he sits anyway)
    assert starters(-0.1)["pct_risk"] == ["A"]  # B sits too, leaving a starting slot open
    assert starters(-0.5)["pct_risk"] == ["A", "B"]  # nobody that bad
