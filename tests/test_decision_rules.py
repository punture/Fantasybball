"""Decision rules that must match Yahoo: IL returners, play-probability lineups, IL stash/activate by status, the
lineup deadline, games already tipped, the Most Categories objective, and merged fantasy weeks."""

from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from fbm.analytics.bank import DrawBank
from fbm.analytics.categories import NINE_CAT
from fbm.analytics.lineup import activate_ready, expected_out_today, il_moves, optimize_day, started_teams
from fbm.analytics.playoffs import build_weeks, merged_periods, playoff_weeks
from fbm.analytics.simulation import COL, STATS, CategoryOdds, MatchupOdds, PlayerModel
from fbm.analytics.streaming import (
    OBJECTIVE_P_WIN,
    InvalidPlan,
    Move,
    StreamContext,
    evaluate,
    plan_streams,
    roster_states,
    waiting_returns,
)
from fbm.analytics.trades import LeagueSim, evaluate_trade
from fbm.analytics.week import (
    first_change_day,
    lineup_deadline,
    p_win_week,
    pivotal_weights,
    plan_team_week,
)
from fbm.analytics.week import waiting_returns as week_waiting
from fbm.constants import C
from fbm.gm import choosers, policy
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer, SlotSpec

D1, D2, D3 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
DAYS = [D1, D2, D3]
HURT = ("C", "UTIL", "IL", "IL+")


def line(**kw) -> np.ndarray:
    row = np.zeros(len(STATS))
    for k, v in kw.items():
        row[COL[k]] = v
    return row


GOOD = line(pts=20, reb=8, ast=5, stl=1, blk=1, tov=2, tpm=2, fgm=8, fga=16, ftm=2, fta=3)
MEH = line(pts=8, reb=3, ast=2, stl=0, blk=0, tov=1, tpm=1, fgm=3, fga=8, ftm=1, fta=2)


def rp(pid, team="AAA", selected="BN", status="", eligible=("C", "UTIL")):
    return RosterPlayer(pid, pid, team, eligible, selected, status)


def every_day(*teams):
    return {d: set(teams) for d in DAYS}


# ---------------------------------------------------------------- A. IL returners in week plans


def test_week_plan_activates_il_returner_into_an_open_spot():
    team = FantasyTeam("t", "T", players=[rp("S", "III", "IL", "O", HURT), rp("B", "BBB", "C")])
    value = {"S": 5.0, "B": 1.0}
    old, _ = plan_team_week(team, DAYS, every_day("III", "BBB"), ["C"], value, {"S": D2})
    assert old.player_games == ["B", "B", "B"]  # no capacity: IL players stay put (old behavior)
    week, plans = plan_team_week(team, DAYS, every_day("III", "BBB"), ["C"], value, {"S": D2}, capacity=2)
    assert list(zip(week.player_games, week.game_dates, strict=True)) == [("B", D1), ("S", D2), ("S", D3)]
    assert plans[1].activated == ("S",) and plans[2].activated == ()  # activated once, stays active
    assert team.players[0].selected == "IL" and team.players[0].status == "O"  # inputs untouched


def test_week_plan_reports_returner_waiting_for_a_spot_or_swaps_with_an_out_player():
    full = FantasyTeam("t", "T", players=[rp("S", "III", "IL", "O", HURT), rp("B", "BBB", "C")])
    week, plans = plan_team_week(full, DAYS, every_day("III", "BBB"), ["C"], {"S": 5.0}, {"S": D2}, capacity=1)
    assert week.player_games == ["B", "B", "B"]
    assert plans[1].waiting == ("S",) and week_waiting(plans) == {"S": D2}
    # An Out active player eligible for the IL slot takes it: the returner plays without a drop.
    out = rp("H", "HHH", "BN", "O", HURT)
    swap = FantasyTeam("t", "T", players=[*full.players, out])
    week, plans = plan_team_week(swap, DAYS, every_day("III", "BBB"), ["C"], {"S": 5.0}, {"S": D2}, capacity=2)
    assert week.player_games == ["B", "S", "S"] and plans[1].activated == ("S",)
    assert plans[1].lineup.assignments["H"] == "IL" and week_waiting(plans) == {}


def test_activate_ready_best_value_first():
    players = [rp("x", selected="IL", eligible=HURT), rp("y", selected="IL+", eligible=HURT), rp("a", selected="C")]
    out, activated, waiting = activate_ready(players, 2, {"x": 1.0, "y": 3.0})
    assert activated == ("y",) and waiting == ("x",)
    assert {p.player_id: p.selected for p in out} == {"x": "IL", "y": "BN", "a": "C"}
    assert players[1].selected == "IL+"
    assert activate_ready([rp("o", selected="IL", status="O", eligible=HURT)], 5)[1:] == ((), ())
    # Out for the season and doubtful players aren't returners: no activation, and no "needs a spot" heads-up.
    for status in ("OFS", "DOUBTFUL", "INJ"):
        held = [rp("x", selected="IL", status=status, eligible=HURT), rp("a", selected="C")]
        assert activate_ready(held, 5)[1:] == ((), ()) and activate_ready(held, 1)[1:] == ((), ())
    # A game-time decision stashed on IL+ can come back; an out-for-season active player can take his slot.
    gtd, ofs = rp("g", selected="IL+", status="GTD", eligible=HURT), rp("f", selected="C", status="OFS", eligible=HURT)
    out, activated, waiting = activate_ready([gtd, ofs], 1)
    assert activated == ("g",) and waiting == () and {p.player_id: p.selected for p in out} == {"g": "BN", "f": "IL+"}


def test_week_plan_locked_days_and_not_playing():
    team = FantasyTeam("t", "T", players=[rp("B", "BBB", "BN"), rp("N", "NNN", "C")])
    week, _ = plan_team_week(team, DAYS, every_day("BBB"), ["C"], {})
    assert week.player_games == ["B", "B", "B"]
    week, _ = plan_team_week(team, DAYS, every_day("BBB"), ["C"], {}, locked_days={D1}, not_playing={D3: {"B"}})
    assert week.game_dates == [D2]  # D1: the lineup as set (B on the bench); D3: B's game doesn't count


def stream_world(**kw):
    models = {"A": PlayerModel("A", GOOD[None]), "S": PlayerModel("S", GOOD[None]), "F": PlayerModel("F", MEH[None])}
    pool = {
        "A": rp("A", "AAA", "C"),
        "S": rp("S", "III", "IL", "O", HURT),
        "F": rp("F", "FFF"),
    }
    args = dict(
        base=[pool["A"], pool["S"]],
        pool=pool,
        days=DAYS,
        playing=every_day("AAA", "III", "FFF"),
        starting_slots=["C", "UTIL"],
        capacity=2,
        value={"A": 3.0, "S": 5.0, "F": 0.0},
        bank=DrawBank(models, n_sims=50),
        opp_totals=np.tile(MEH * 3, (50, 1)),
        categories=NINE_CAT,
        weights={c.key: 1.0 for c in NINE_CAT},
        return_dates={"S": D2},
    )
    return StreamContext(**{**args, **kw})


def test_streaming_activates_returner_and_accounts_for_the_spot_he_needs():
    ctx = stream_world()
    states = roster_states(ctx, [])
    assert states[D1].held == {"S"} and states[D2].held == frozenset()
    _, starts = evaluate(ctx, [])
    assert starts == 5  # A x3, S from his return day x2
    # A streamer added into the open spot first blocks the returner: he waits for a spot from D2.
    assert waiting_returns(ctx, [Move(D1, "F", None)]) == {"S": D2}
    # Once the returner holds the spot, an add without a drop is impossible.
    with pytest.raises(InvalidPlan, match="roster full"):
        roster_states(ctx, [Move(D2, "F", None)])
    plan = plan_streams(ctx, ["F"], [], adds_available=1)
    assert plan.moves == [] and plan.waiting_returns == {}  # a MEH streamer isn't worth the returner's games


# ---------------------------------------------------------------- A. IL players in the league sim (trades)


def test_league_sim_counts_il_players_at_availability_and_trading_one_costs_the_partner():
    models = {
        "a1": PlayerModel("a1", GOOD[None]),
        "a2": PlayerModel("a2", MEH[None]),
        "b1": PlayerModel("b1", MEH[None]),
        "b2": PlayerModel("b2", GOOD[None], play_prob=0.5),  # on IL, back for half the remaining games
    }
    cats = [c.key for c in NINE_CAT]
    values = pd.DataFrame({pid: {f"g_{k}": 0.0 for k in cats} for pid in models}).T
    values["g_total"] = [2.0, -1.0, -1.0, 1.0]
    teams = [
        FantasyTeam("t1", "Me", players=[rp("a1", selected="C"), rp("a2", selected="UTIL")]),
        FantasyTeam("t2", "Them", players=[rp("b1", selected="C"), rp("b2", selected="IL", status="O", eligible=HURT)]),
    ]
    slots = (SlotSpec("C", 1, True), SlotSpec("UTIL", 1, True), SlotSpec("IL", 1, False))
    sim = LeagueSim(LeagueState("L", "T", 2027, "t1", teams, slots=slots), DrawBank(models, n_sims=400), values)
    assert sim.il == {"t1": [], "t2": ["b2"]} and sim.availability("b2") == 0.5
    pts = COL["pts"]
    assert sim.totals["t2"][:, pts].mean() > sim.team_totals(["b1"])[:, pts].mean()  # b2 counts at his share
    assert sim.place_received([], ["b2", "a2"]) == (["a2"], ["b2"])  # IL player into an open IL slot
    ev = evaluate_trade(sim, "t1", "t2", ["a2"], ["b2"], [])
    assert ev.me.drops == [] and ev.them.drops == []  # b2 lands on my IL: no active spot needed
    assert ev.them.expected_delta < 0 < ev.me.expected_delta  # they give up b2's games, not nothing


# ---------------------------------------------------------------- B. play probability and explicit benching


def test_healthy_player_with_a_game_beats_a_game_time_decision_for_a_tight_slot():
    players = [rp("gtd", status="GTD"), rp("ok")]
    res = optimize_day(players, ["C"], {"gtd", "ok"}, {"gtd": 10.0, "ok": 0.0})
    assert res.assignments == {"gtd": "BN", "ok": "C"} and res.benched_with_games == ("gtd",)
    # A game-time decision with a game still beats a better player without one ...
    res = optimize_day([rp("gtd", status="GTD"), rp("idle")], ["C"], {"gtd"}, {"idle": 50.0})
    assert res.assignments["gtd"] == "C"
    # ... and starts when there is room for both.
    assert optimize_day(players, ["C", "UTIL"], {"gtd", "ok"}, {}).starters_with_games == 2


def test_sit_benches_a_player_even_with_an_open_slot_but_not_a_locked_one():
    players = [rp("brick", selected="C"), rp("other")]
    res = optimize_day(players, ["C", "UTIL"], {"brick", "other"}, {"brick": 5.0}, sit={"brick"})
    assert res.assignments["brick"] == "BN" and res.starters_with_games == 1
    assert res.benched_with_games == ("brick",)
    locked = optimize_day(players, ["C", "UTIL"], {"brick"}, {}, locked={"brick"}, sit={"brick"})
    assert locked.assignments["brick"] == "C"


# ---------------------------------------------------------------- C. IL stash / activation follow injury status

IL_COUNTS = {"IL": 1, "IL+": 1}


def test_expected_out_today():
    assert expected_out_today(rp("o", status="O")) and expected_out_today(rp("f", status="OFS"))
    assert expected_out_today(rp("d", status="DOUBTFUL"), {"d"})  # doubtful: at most il_stash_max_play_prob
    assert not expected_out_today(rp("g", status="GTD"), {"g"})
    assert expected_out_today(rp("g", status="GTD"), set())  # no game today
    assert not expected_out_today(rp("g", status="GTD"))  # games unknown: assume he may play
    assert not expected_out_today(rp("h"), set())  # healthy is never stashed
    assert float(C("lineup.il_stash_max_play_prob")) >= float(C("sim.play_prob.doubtful"))


def test_il_stash_respects_status_and_todays_games():
    gtd = rp("g", selected="C", status="GTD", eligible=HURT)
    assert il_moves([gtd], IL_COUNTS, playing={"g"}) == []  # likely to play tonight: stays active
    assert il_moves([gtd], IL_COUNTS) == []
    no_game = il_moves([gtd], IL_COUNTS, playing=set())
    assert [(m.player_id, m.to_slot, m.blocked) for m in no_game] == [("g", "IL", False)]
    out = rp("o", selected="C", status="O", eligible=HURT)
    assert [m.to_slot for m in il_moves([out], IL_COUNTS, playing={"o"})] == ["IL"]


def test_il_player_whose_status_improved_is_activated_when_he_has_a_game():
    back = rp("r", selected="IL+", status="GTD", eligible=HURT)
    moves = il_moves([back, rp("a", selected="C")], IL_COUNTS, capacity=2, playing={"r"})
    assert [(m.player_id, m.to_slot, m.blocked) for m in moves] == [("r", "BN", False)]
    assert "with a game today" in moves[0].reason
    assert il_moves([back], IL_COUNTS, capacity=2, playing=set()) == []  # no game: no reason to activate
    assert il_moves([back], IL_COUNTS, capacity=2) == []  # games unknown
    # Full roster: he is still IL-eligible, so nothing is blocked (no il_moves_blocked alert): he stays on IL.
    assert il_moves([back, rp("a", selected="C")], IL_COUNTS, capacity=1, playing={"r"}) == []
    # ... unless an active player ruled out can take his IL+ slot.
    hurt = rp("h", selected="C", status="O", eligible=HURT)
    swap = il_moves([back, hurt], {"IL+": 1}, capacity=1, playing={"r", "h"})
    assert [(m.player_id, m.to_slot, m.blocked) for m in swap] == [("h", "IL+", False), ("r", "BN", False)]
    # A healthy player no longer IL-eligible with a full roster is the blocked (compliance) case.
    healthy = rp("x", selected="IL", eligible=("C", "UTIL"))
    full = il_moves([healthy, rp("a", selected="C")], IL_COUNTS, capacity=1, playing={"x"})
    assert [(m.player_id, m.blocked) for m in full] == [("x", True)] and "roster is full" in full[0].reason


# ---------------------------------------------------------------- D. lineup deadline


def test_lineup_deadline_parsing_is_defensive():
    assert lineup_deadline(None) == "today"
    assert lineup_deadline({"weekly_deadline": ""}) == "today"
    assert lineup_deadline({"weekly_deadline": "intraday"}) == "today"
    assert lineup_deadline({"settings": {"weekly_deadline": "tomorrow"}}) == "tomorrow"
    assert lineup_deadline({"weekly_deadline": "Daily - Tomorrow"}) == "tomorrow"
    assert lineup_deadline({"weekly_deadline": "weird"}) == "today"
    assert lineup_deadline({"weekly_deadline": "intraday"}, "tomorrow") == "tomorrow"
    assert lineup_deadline({"weekly_deadline": "tomorrow"}, "today") == "today"
    assert lineup_deadline({"weekly_deadline": "tomorrow"}, "auto") == "tomorrow"
    assert first_change_day(D1, "today") == D1 and first_change_day(D1, "tomorrow") == D2


def test_daily_tomorrow_moves_start_next_day_and_todays_lineup_stands():
    base = stream_world(base=[rp("A", "AAA", "C"), rp("B", "FFF", "BN")], return_dates={}, capacity=3)
    base.pool = {**base.pool, "B": rp("B", "FFF", "BN")}
    assert evaluate(base, [])[1] == 6  # A and B start every day
    tomorrow = replace(base, first_change_day=D2, _lineups={})
    assert evaluate(tomorrow, [])[1] == 5  # D1 lineup as set: B stays on the bench
    with pytest.raises(InvalidPlan, match="take effect from"):
        roster_states(tomorrow, [Move(D1, "F", None)])
    plan = plan_streams(tomorrow, ["F"], [], adds_available=1, min_gain=-1.0)
    assert [m.move.day for m in plan.moves] == [D2]


# ---------------------------------------------------------------- E. games already tipped


def test_added_player_gets_no_credit_for_a_game_that_already_tipped():
    ctx = stream_world(base=[rp("A", "AAA", "C")], return_dates={}, tipped={D1: {"FFF", "AAA"}})
    assert ctx.add_game_days("F") == [D2, D3] and ctx.game_days("F") == DAYS
    _, starts = evaluate(ctx, [Move(D1, "F", None)])
    assert starts == 3 + 2  # A keeps his D1 game (already on the roster); F only from D2
    untipped = stream_world(base=[rp("A", "AAA", "C")], return_dates={})
    assert evaluate(untipped, [Move(D1, "F", None)])[1] == 6
    plan = plan_streams(ctx, ["F"], [], adds_available=1, min_gain=-1.0)
    assert plan.moves[0].add_games == 2


def test_started_teams_uses_state_and_tip_time_and_tolerates_bad_times():
    now = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)
    games = [
        {"start_utc": "2026-11-17T00:30:00+00:00", "state": "pre", "home": "BOS", "away": "NY"},
        {"start_utc": "2026-11-17T03:00:00", "state": "pre", "home": "LAL", "away": "GS"},  # naive = UTC
        {"start_utc": "x", "state": "in", "home": "POR", "away": "PHX"},
        {"start_utc": "x", "state": "pre", "home": "MIA", "away": "ORL"},
    ]
    assert started_teams(games, now) == {"BOS", "NY", "POR", "PHX"}


# ---------------------------------------------------------------- F. Most Categories objective


def odds_of(p_majority: float, p_loss: float) -> MatchupOdds:
    cats = tuple(CategoryOdds(c, 0.5, 0.0, 0.5, 0, 0) for c in NINE_CAT)
    return MatchupOdds(cats, 4.5, p_majority, tuple([0.0] * 10), p_loss)


def test_p_win_week_counts_a_tied_week_half():
    assert p_win_week(odds_of(0.5, 0.3)) == pytest.approx(0.6)
    assert p_win_week(odds_of(1.0, 0.0)) == pytest.approx(1.0)


def test_pivotal_weights_measure_how_often_a_category_decides_the_week():
    cats = [c for c in NINE_CAT if c.key in ("pts", "reb", "ast")]
    me, opp = np.zeros((4, len(STATS))), np.full((4, len(STATS)), 5.0)
    me[:, COL["pts"]] = [10, 0, 10, 10]
    me[:, COL["reb"]] = [10, 10, 0, 10]
    me[:, COL["ast"]] = [0, 0, 10, 10]
    w = pivotal_weights(me, opp, cats)
    assert w == pytest.approx({"pts": 1.0, "reb": 1 / 3, "ast": 2 / 3})
    assert pivotal_weights(me, opp, cats, punts={"ast"}) == pytest.approx({"pts": 1.0, "reb": 1 / 3, "ast": 0.0})


def close_opponent() -> np.ndarray:
    # vs A alone (3 x GOOD) I win 4 categories and lose 5; adding F (3 x MEH) wins 5, loses 3, ties FT%.
    opp = line(pts=70, reb=30, ast=18, stl=4, blk=2, tov=7, tpm=8, fgm=24, fga=50, ftm=6, fta=10)
    return np.tile(opp, (50, 1))


@pytest.fixture
def constants_file(tmp_path, monkeypatch):
    from fbm import constants

    path = tmp_path / "constants.toml"
    monkeypatch.setattr(constants, "OVERRIDES_PATH", path)
    monkeypatch.setattr(constants, "_cache", {"mtime": None, "values": {}})
    return path


def test_stream_planner_maximizes_p_win_in_most_categories_leagues(constants_file):
    ctx = stream_world(base=[rp("A", "AAA", "C")], return_dates={}, opp_totals=close_opponent())
    p_win = replace(ctx, objective=OBJECTIVE_P_WIN, _lineups={})
    plan = plan_streams(p_win, ["F"], [], adds_available=1, min_gain=0.0)
    assert plan.objective == OBJECTIVE_P_WIN and plan.baseline_score == pytest.approx(0.0)
    m = plan.moves[0]
    assert m.gain == pytest.approx(m.p_win_gain) == pytest.approx(1.0) and plan.final_score == pytest.approx(1.0)
    assert m.expected_cats_gain == pytest.approx(1.5)  # 4 -> 5 wins + a tie
    # Default thresholds are category units, converted to P(win) units (x stream.p_win_per_cat = 0.25):
    # a 3-category minimum blocks the +1.5-category move but not the +1.0 P(win) one (3 x 0.25 = 0.75).
    constants_file.write_text('"stream.min_gain" = 3.0\n', encoding="utf-8")
    assert plan_streams(replace(ctx, _lineups={}), ["F"], [], adds_available=1).moves == []
    assert len(plan_streams(replace(p_win, _lineups={}), ["F"], [], adds_available=1).moves) == 1


P = {k: p.default for k, p in policy.REGISTRY.items()}


def _decision(options, **situation):
    base = {"odds": {"pts": 0.5}, "punts": [], "weekday": 4, "adds_left": 3}
    return {"id": 1, "kind": "stream_plan", "situation": {**base, **situation}, "options": options}


def test_chooser_values_options_by_p_win_in_most_categories_leagues():
    options = [
        {"id": "stand_pat", "label": "s", "stand_pat": True, "cat_scores": {"pts": 0.0}, "p_win_delta": 0.0},
        {"id": "cats", "label": "c", "adds": 1, "cat_scores": {"pts": 0.5}, "p_win_delta": 0.01},
        {"id": "win", "label": "w", "adds": 1, "cat_scores": {"pts": 0.2}, "p_win_delta": 0.06},
    ]
    assert choosers.choose(_decision(options), P) == "cats"  # H2H Categories: expected categories
    assert choosers.choose(_decision(options, objective="p_win", p_win_per_cat=0.25), P) == "win"
    # Category-unit thresholds are converted to P(win) units: +0.01 is below 0.05 x 0.25 = 0.0125 -> stand pat;
    # a ROS cost of 1 G costs 0.5 x 0.25 = 0.125 P(win), more than the +0.06 the move brings.
    assert P["policy.stream_min_gain"] == 0.05 and P["policy.ros_penalty"] == 0.5
    small = [options[0], {**options[1], "id": "tiny", "p_win_delta": 0.01}]
    assert choosers.choose(_decision(small, objective="p_win", p_win_per_cat=0.25), P) == "stand_pat"
    costly = [options[0], {**options[2], "ros_cost": 1.0}]
    assert choosers.choose(_decision(costly, objective="p_win", p_win_per_cat=0.25), P) == "stand_pat"
    # An option without p_win_delta falls back to category scores.
    legacy = [options[0], {"id": "old", "label": "o", "adds": 1, "cat_scores": {"pts": 0.5}}]
    assert choosers.choose(_decision(legacy, objective="p_win", p_win_per_cat=0.25), P) == "old"


def test_chooser_keeps_category_units_when_options_carry_no_p_win_delta():
    # Trades, claims and IL options are scored in categories even in Most Categories leagues: the stream_min_gain
    # threshold must stay in categories too (not x p_win_per_cat), or a +0.02-category trade would clear it.
    options = [
        {"id": "stand", "label": "s", "stand_pat": True, "cat_scores": {"pts": 0.0}},
        {"id": "trade", "label": "t", "cat_scores": {"pts": 0.02}},
    ]
    trade = {"id": 1, "kind": "trade", "situation": {"odds": {"pts": 0.5}, "punts": []}, "options": options}
    assert choosers.choose(trade, P) == "stand"
    trade["situation"].update(objective="p_win", p_win_per_cat=0.25)
    assert choosers.choose(trade, P) == "stand"
    # A stand-pat option logged without p_win_delta still counts as no change in P(win).
    mixed = [options[0], {"id": "win", "label": "w", "adds": 1, "cat_scores": {"pts": -5.0}, "p_win_delta": 0.02}]
    assert choosers.choose(_decision(mixed, objective="p_win", p_win_per_cat=0.25), P) == "win"


def test_chooser_uses_logged_category_weights():
    lineup = {
        "id": 1,
        "kind": "lineup",
        "situation": {"odds": {"pts": 0.5, "reb": 0.5}, "cat_weights": {"pts": 0.1, "reb": 1.0}},
        "options": [
            {"id": "pts", "label": "p", "cat_scores": {"pts": 3.0, "reb": 0.0}},
            {"id": "reb", "label": "r", "cat_scores": {"pts": 0.0, "reb": 1.0}},
        ],
    }
    assert choosers.choose(lineup, P) == "reb"
    lineup["situation"].pop("cat_weights")
    assert choosers.choose(lineup, P) == "pts"


# ---------------------------------------------------------------- G. merged fantasy weeks


def test_merged_periods_parse_defensively():
    assert merged_periods("2026-11-30..2026-12-13, bad, 2027-02-28..2027-02-15,2027-02-15..2027-02-28") == [
        (date(2026, 11, 30), date(2026, 12, 13)),
        (date(2027, 2, 15), date(2027, 2, 28)),
    ]
    assert merged_periods("") == []


def test_yahoo_2026_27_weeks_with_merged_nba_cup_and_all_star_weeks():
    weeks = build_weeks(date(2026, 10, 20), date(2027, 4, 11), merged_periods())
    by_number = {w.number: (w.start, w.end) for w in weeks}
    assert by_number[1] == (date(2026, 10, 19), date(2026, 10, 25))
    assert by_number[7] == (date(2026, 11, 30), date(2026, 12, 13))
    assert by_number[8] == (date(2026, 12, 14), date(2026, 12, 20))
    assert by_number[17] == (date(2027, 2, 15), date(2027, 2, 28))
    assert [(w.number, w.start, w.end) for w in playoff_weeks(weeks)] == [
        (20, date(2027, 3, 15), date(2027, 3, 21)),
        (21, date(2027, 3, 22), date(2027, 3, 28)),
        (22, date(2027, 3, 29), date(2027, 4, 4)),
    ]
    settings = {"end_week": "22", "settings": {"playoff_start_week": "20", "uses_playoff": "1"}}
    assert [w.start for w in playoff_weeks(weeks, settings)] == [
        date(2027, 3, 15),
        date(2027, 3, 22),
        date(2027, 3, 29),
    ]
    # Without the merges the same week numbers land a week or two early.
    plain = {w.number: w.start for w in build_weeks(date(2026, 10, 20), date(2027, 4, 11))}
    assert plain[20] == date(2027, 3, 1)


def test_known_week_bounds_anchor_the_numbering():
    # The calendar calls Nov 2-8 week 3; a league whose Yahoo week 1 was a long opening week (Oct 20 - Nov 1) reports
    # it as week 2. The snapshot's bounds re-number every later week (the NBA Cup week becomes week 6).
    anchor = (2, date(2026, 11, 2), date(2026, 11, 8))
    weeks = build_weeks(date(2026, 10, 20), date(2027, 4, 11), merged_periods(), anchor=anchor)
    by_number = {w.number: (w.start, w.end) for w in weeks}
    assert by_number[2] == (date(2026, 11, 2), date(2026, 11, 8))
    assert by_number[6] == (date(2026, 11, 30), date(2026, 12, 13))
    assert by_number[1] == (date(2026, 10, 26), date(2026, 11, 1)) and 0 not in by_number
    reversed_bounds = (1, date(2026, 11, 8), date(2026, 11, 1))
    assert build_weeks(date(2026, 10, 20), date(2026, 11, 8), anchor=reversed_bounds)[0].number == 1  # ignored


def test_expected_cats_gain_counts_decided_categories_weighted_zero_but_not_punts():
    # policy.weight_floor may be 0: a decided (not punted) category then weighs 0 in the search but still counts in
    # gain_expected_cats ("unweighted change over non-punted categories").
    from fbm.analytics.streaming import _punted, expected_cats

    odds = odds_of(0.5, 0.3)
    assert expected_cats(odds, ()) == pytest.approx(4.5)
    assert expected_cats(odds, {"pts"}) == pytest.approx(4.0)
    zero = {c.key: (0.0 if c.key == "blk" else 1.0) for c in NINE_CAT}
    assert _punted(stream_world(weights=zero)) == {"blk"}  # no explicit punts: weight 0 means punted
    assert _punted(stream_world(weights=zero, punts=frozenset())) == frozenset()
