"""Issue #14: the Monday after Yahoo's week ended but before Yahoo rolls over (it still reports the old week).

Week number, window and opponent must come from one resolver (fbm.league.weeks) so every tool agrees; all offline."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from fbm.gm import lookahead as la
from fbm.gm import services as svc
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer, SlotSpec
from fbm.league.weeks import WeekInfo, effective_week, next_week_info, week_info_from_matchups
from fbm.store import db
from fbm.yahoo.league_reads import Matchup, MatchupTeam

L = "466.l.1"
ME, SHOHAKU, DIMEBAG = f"{L}.t.1", f"{L}.t.2", f"{L}.t.3"
MONDAY = date(2026, 11, 9)  # Yahoo week 3 (11-02..11-08) is over; real week 4 is 11-09..11-15 vs dimebag


def _matchups(week, start, end, opp, status="preevent"):
    teams = tuple(MatchupTeam(k, k, {}, None) for k in (ME, opp))
    return [Matchup(week, start, end, status, False, teams)]


SCHEDULE = {
    4: _matchups(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG),
    5: _matchups(5, date(2026, 11, 16), date(2026, 11, 22), SHOHAKU),
}


def _league(week=3, start=date(2026, 11, 2), end=date(2026, 11, 8), opp=SHOHAKU, source="yahoo") -> LeagueState:
    me = FantasyTeam(ME, "Glass Cleaners", "Quin", [RosterPlayer("a", "A Guard", "BOS", ("PG", "UTIL"), "PG")])
    sho = FantasyTeam(SHOHAKU, "Team Shohoku", "Ryo", [RosterPlayer("b", "B Wing", "LAL", ("SF", "UTIL"), "UTIL")])
    dime = FantasyTeam(DIMEBAG, "dimebag", "Sam", [RosterPlayer("c", "C Big", "MIA", ("C", "UTIL"), "UTIL")])
    slots = (SlotSpec("PG", 1, True), SlotSpec("UTIL", 2, True), SlotSpec("BN", 2, False), SlotSpec("IL", 1, False))
    return LeagueState(
        L,
        "L",
        2027,
        ME,
        [me, sho, dime],
        slots=slots,
        source=source,
        week=week,
        week_start=start,
        week_end=end,
        opponent_key=opp,
        banked={ME: {"pts": 500.0}, SHOHAKU: {"pts": 480.0}},
        adds_used={ME: 3},
        fetched_at="2026-11-09T07:10:00+00:00",
        matchups=[
            {"week": 3, "status": "midevent", "teams": [{"team_key": ME, "name": "Glass Cleaners", "stats": {}}]}
        ],
    )


def _yahoo_schedule(monkeypatch, schedule=SCHEDULE, today=MONDAY):
    def matchups(league, week):
        if week not in schedule:
            raise RuntimeError("Yahoo down")
        return schedule[week]

    monkeypatch.setattr(svc, "_yahoo_week_matchups", matchups)
    monkeypatch.setattr(svc, "nba_today", lambda: today)


# ------------------------------------------------------------------ the pure resolver


def _lookup(week):
    return week_info_from_matchups(week, SCHEDULE.get(week, []), ME)


def test_week_info_from_matchups_bounds_and_opponent():
    assert _lookup(4) == WeekInfo(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG, "yahoo schedule")
    assert _lookup(9) is None  # no matchups: nothing to build from
    bye = [Matchup(4, date(2026, 11, 9), date(2026, 11, 15), "preevent", False, (MatchupTeam("x", "x", {}, None),))]
    assert week_info_from_matchups(4, bye, ME).opponent_key is None  # a bye still has the week's bounds


def test_effective_week_monday_before_rollover_uses_next_yahoo_week():
    yahoo = WeekInfo(3, date(2026, 11, 2), date(2026, 11, 8), SHOHAKU)
    eff = effective_week(yahoo, MONDAY, _lookup)
    assert (eff.week.number, eff.week.start, eff.week.end, eff.week.opponent_key) == (
        4,
        date(2026, 11, 9),
        date(2026, 11, 15),
        DIMEBAG,
    )
    assert eff.rolled_from == yahoo and "has not rolled over" in eff.note and "week 4" in eff.note
    nxt = next_week_info(eff.week, _lookup)
    assert (nxt.number, nxt.start, nxt.end) == (5, date(2026, 11, 16), date(2026, 11, 22))


@pytest.mark.parametrize("today", [date(2026, 11, 2), date(2026, 11, 5), date(2026, 11, 8)])
def test_effective_week_mid_week_and_last_day_unchanged(today):
    yahoo = WeekInfo(3, date(2026, 11, 2), date(2026, 11, 8), SHOHAKU)
    eff = effective_week(yahoo, today, _lookup)
    assert eff.week is yahoo and eff.rolled_from is None and eff.note == ""


def test_effective_week_after_yahoo_rolled_over_unchanged():
    yahoo = WeekInfo(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG)
    assert effective_week(yahoo, MONDAY, _lookup).rolled_from is None


def test_effective_week_without_schedule_falls_back_to_calendar_with_unknown_opponent():
    yahoo = WeekInfo(3, date(2026, 11, 2), date(2026, 11, 8), SHOHAKU)
    eff = effective_week(yahoo, MONDAY, lambda w: None)
    assert (eff.week.number, eff.week.start, eff.week.end) == (4, date(2026, 11, 9), date(2026, 11, 15))
    assert eff.week.opponent_key is None and eff.week.source == "calendar" and "opponent is unknown" in eff.note


def test_effective_week_ignores_inconsistent_schedule_entry():
    yahoo = WeekInfo(3, date(2026, 11, 2), date(2026, 11, 8), SHOHAKU)
    stale = lambda w: WeekInfo(w, date(2026, 11, 2), date(2026, 11, 8), DIMEBAG)  # noqa: E731 - overlaps week 3
    eff = effective_week(yahoo, MONDAY, stale)
    assert eff.week.start == date(2026, 11, 9) and eff.week.opponent_key is None


def test_effective_week_rolls_several_weeks_and_keeps_long_yahoo_weeks():
    yahoo = WeekInfo(3, date(2026, 11, 2), date(2026, 11, 8), SHOHAKU)
    assert effective_week(yahoo, date(2026, 11, 17), _lookup).week.number == 5
    two_week = {4: _matchups(4, date(2026, 11, 9), date(2026, 11, 22), DIMEBAG)}  # e.g. around the All-Star break
    eff = effective_week(yahoo, date(2026, 11, 20), lambda w: week_info_from_matchups(w, two_week.get(w, []), ME))
    assert (eff.week.number, eff.week.end) == (4, date(2026, 11, 22))


def test_undated_pairing_keeps_the_opponent_on_calendar_dates():
    """Flaim sends no dates for a week that isn't current yet: Yahoo's pairing must survive the Monday rollover."""
    undated = {5: _matchups(5, None, None, SHOHAKU)}
    lookup = lambda w: week_info_from_matchups(w, undated.get(w, []), ME)  # noqa: E731
    assert lookup(5) == WeekInfo(5, None, None, SHOHAKU, "yahoo schedule")
    yahoo = WeekInfo(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG)
    eff = effective_week(yahoo, date(2026, 11, 16), lookup)
    assert eff.week == WeekInfo(5, date(2026, 11, 16), date(2026, 11, 22), SHOHAKU, "yahoo pairing + calendar")
    assert "opponent from Yahoo's schedule" in eff.note and "unknown" not in eff.note
    # an undated NBA Cup week takes the merged calendar period (fantasy_week), still with Yahoo's opponent
    cup = {7: _matchups(7, None, None, SHOHAKU)}
    week6 = WeekInfo(6, date(2026, 11, 23), date(2026, 11, 29), DIMEBAG)
    nxt = next_week_info(week6, lambda w: week_info_from_matchups(w, cup.get(w, []), ME))
    assert (nxt.start, nxt.end, nxt.opponent_key) == (date(2026, 11, 30), date(2026, 12, 13), SHOHAKU)


def test_matchups_of_another_week_never_name_the_opponent():
    """A feed answering a week-5 request with week 4's scoreboard must not make week 4's opponent week 5's."""
    current = _matchups(4, None, None, DIMEBAG)
    assert week_info_from_matchups(5, current, ME) is None
    nxt = next_week_info(WeekInfo(4, date(2026, 11, 9), date(2026, 11, 15)), lambda w: None)
    assert nxt.opponent_key is None and nxt.source == "calendar"


def test_roll_week_on_flaims_real_undated_scoreboard(monkeypatch):
    """The real Flaim week-5 scoreboard (no dates) while the snapshot still says week 4: the opponent is kept."""
    import json
    from pathlib import Path

    from fbm.flaim.reads import parse_matchups

    lk = "999.l.12345"
    me, opp = f"{lk}.t.8", f"{lk}.t.5"
    raw = json.loads((Path(__file__).parent / "fixtures/flaim/real/get_matchups_week5.json").read_text("utf-8"))
    week5 = parse_matchups(raw, lk)
    assert all(m.week_start is None for m in week5)
    teams = [FantasyTeam(k, k, k, []) for k in (me, opp)]
    league = replace(_league(4, date(2026, 11, 9), date(2026, 11, 15), None), league_key=lk, my_team_key=me)
    league = replace(league, teams=teams)
    _yahoo_schedule(monkeypatch, schedule={5: week5}, today=date(2026, 11, 16))
    rolled = svc._roll_week(league)
    assert (rolled.week, rolled.week_start, rolled.week_end) == (5, date(2026, 11, 16), date(2026, 11, 22))
    assert rolled.opponent_key == opp and svc._default_opponent(rolled).team_key == opp


# ------------------------------------------------------------------ the league view every tool shares


def test_roll_week_moves_number_window_and_opponent_together(monkeypatch):
    _yahoo_schedule(monkeypatch)
    raw = _league()
    league = svc._roll_week(raw)
    assert (league.week, league.week_start, league.week_end, league.opponent_key) == (
        4,
        date(2026, 11, 9),
        date(2026, 11, 15),
        DIMEBAG,
    )
    assert league.rolled_from_week == 3 and "has not rolled over" in league.rollover_note
    assert league.banked == {} and league.adds_used == {}  # the finished week's numbers don't carry over
    assert league.matchups == raw.matchups  # Yahoo's own scoreboard is untouched
    assert (raw.week, raw.opponent_key) == (3, SHOHAKU)  # the snapshot object isn't mutated


def test_roll_week_leaves_normal_weeks_demo_and_post_rollover_alone(monkeypatch):
    _yahoo_schedule(monkeypatch, today=date(2026, 11, 5))
    mid = _league()
    assert svc._roll_week(mid) is mid  # mid-week
    _yahoo_schedule(monkeypatch)
    after = _league(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG)
    assert svc._roll_week(after) is after  # Yahoo already rolled
    demo = _league(source="demo")
    assert svc._roll_week(demo) is demo and demo.rolled_from_week is None  # the demo league has no schedule


def test_roll_week_without_yahoo_schedule_has_unknown_opponent(monkeypatch):
    _yahoo_schedule(monkeypatch, schedule={})
    league = svc._roll_week(_league())
    assert (league.week, league.week_start, league.week_end, league.opponent_key) == (
        4,
        date(2026, 11, 9),
        date(2026, 11, 15),
        None,
    )
    with pytest.raises(svc.GMError, match="opponent is unknown"):
        svc._default_opponent(league)
    assert svc._default_opponent(_league()).team_key == SHOHAKU  # an unrolled league keeps its behaviour


def test_active_league_applies_rollover_for_yahoo_only(monkeypatch):
    _yahoo_schedule(monkeypatch)
    monkeypatch.setattr(svc, "load_settings", lambda: SimpleNamespace(league_source="yahoo"))
    monkeypatch.setattr("fbm.league.yahoo_league.load_or_refresh", lambda max_age: _league())
    league = svc.active_league()
    assert (league.week, league.opponent_key, league.rolled_from_week) == (4, DIMEBAG, 3)


def _games_conn():
    return db.connect(":memory:")


def test_week_window_agrees_with_rolled_league_and_prefers_yahoo_bounds(monkeypatch):
    _yahoo_schedule(monkeypatch)
    conn = _games_conn()
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    assert svc._week_window() == (MONDAY, date(2026, 11, 9), date(2026, 11, 15))
    long_week = replace(league, week_end=date(2026, 11, 22))  # Yahoo's own bounds win over Monday-Sunday
    monkeypatch.setattr(svc, "active_league", lambda: long_week)
    assert svc._week_window() == (MONDAY, date(2026, 11, 9), date(2026, 11, 22))


def test_league_overview_fantasy_week_matches_rolled_league(monkeypatch):
    _yahoo_schedule(monkeypatch)
    conn = _games_conn()
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc.memory, "current_strategy", lambda: {})
    monkeypatch.setattr("fbm.league.yahoo_league.last_refresh_error", lambda: "")
    fw = svc.league_overview()["fantasy_week"]
    assert (fw["number"], fw["start"], fw["end"]) == (4, "2026-11-09", "2026-11-15")
    assert fw["rolled_over_from_yahoo_week"] == 3 and "has not rolled over" in fw["note"]


def test_scoreboard_keeps_yahoos_finished_week_labelled(monkeypatch):
    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    res = svc.scoreboard_tool()
    assert res["week"] == 3 and "has not rolled over" in res["note"]
    assert [m["week"] for m in res["matchups"]] == [3]


def test_default_opponent_and_simulate_matchup_target_the_new_week(monkeypatch):
    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    assert svc._default_opponent(league).name == "dimebag"
    assert svc._team(league, None).name == "dimebag"
    seen = {}

    def fake_matchup(lg, me, opp, start, n, **kw):
        seen.update(opp=opp.team_key, start=start, week=lg.week)
        cat = SimpleNamespace(label="PTS", kind="count")
        o = SimpleNamespace(category=cat, mean_a=1.0, mean_b=1.0, p_win=0.5, p_tie=0.0, p_loss=0.5)
        odds = SimpleNamespace(categories=[o], expected_score=4.5, p_majority=0.5, swing=lambda: [])
        mw = SimpleNamespace(player_games=[], banked={})
        return odds, mw, mw, date(2026, 11, 9), date(2026, 11, 15), {}, [], []

    monkeypatch.setattr(svc, "ctx", lambda: SimpleNamespace(hashtag=False))
    monkeypatch.setattr(svc, "_matchup", fake_matchup)
    monkeypatch.setattr(svc, "_opponent_adds", lambda *a, **k: (0, "none"))
    monkeypatch.setattr(svc, "_week_window", lambda day=None: (MONDAY, date(2026, 11, 9), date(2026, 11, 15)))
    res = svc.simulate_matchup_tool()
    assert seen == {"opp": DIMEBAG, "start": None, "week": 4}
    assert res["opponent"] == "dimebag" and res["window_et"] == "2026-11-09..2026-11-15"
    with pytest.raises(svc.GMError, match="only for this week's scheduled opponent"):
        svc.simulate_matchup_tool(opponent="shohoku", record=True)  # last week's opponent is no longer "this week"


def test_weekly_plan_looks_up_ht_totals_for_the_new_week_and_explains_missing_ones(monkeypatch):
    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "ctx", lambda: SimpleNamespace(hashtag=True))
    monkeypatch.setattr(svc, "_conn", lambda: None)
    monkeypatch.setattr(svc._policy, "get", lambda conn, key: "STREAM7")
    monkeypatch.setattr(svc, "_log_weekly_plan", lambda *a, **k: {"status": {"pts": "hold"}})
    seen = {}

    def fake_odds(lg, opp, n):
        seen.update(week=lg.week, opp=opp.team_key)
        return {"p_win": {"pts": 0.6}}

    monkeypatch.setattr(svc, "_ht_week_odds", fake_odds)
    res = svc.weekly_plan(opponent="dimebag")
    assert seen == {"week": 4, "opp": DIMEBAG} and res["opponent"] == "dimebag"

    def missing(lg, opp, n):
        raise svc.GMError(f"No Hashtag H2H Planner totals for week {lg.week} vs {opp.name} (view STREAM7)")

    monkeypatch.setattr(svc, "_ht_week_odds", missing)
    with pytest.raises(svc.GMError) as err:
        svc.weekly_plan(opponent="dimebag")
    text = str(err.value)
    assert "week 4 vs dimebag" in text and "has not rolled over" in text and "fbm hashtag import" in text


def test_hint_only_added_when_rolled():
    plain = svc.GMError("No Hashtag H2H Planner totals")
    assert svc._rollover_hint(_league(), plain) is plain


# ------------------------------------------------------------------ next_week


def _patch_next_week(monkeypatch, league):
    conn = db.connect(":memory:")
    for i, (d, home, away) in enumerate(
        [("2026-11-10", "BOS", "LAL"), ("2026-11-16", "BOS", "MIA"), ("2026-11-17", "LAL", "MIA")]
    ):
        conn.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(i), 2027, 2, d + "T00:00Z", d, home, away, "pre", 0, 0, 1, 0, d),
        )
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(svc, "_week_window", lambda day=None: (MONDAY, date(2026, 11, 9), date(2026, 11, 15)))
    monkeypatch.setattr(svc, "ctx", lambda: SimpleNamespace(return_dates=lambda conn=None: {}))
    monkeypatch.setattr(svc, "_strategy_punts", lambda punts: ())
    monkeypatch.setattr(svc, "_value", lambda c, punts: pd.Series({"a": 1.0, "b": 2.0, "c": 0.5}))
    monkeypatch.setattr(svc, "rankings", lambda **kw: {"players": []})
    windows = []

    def fake_matchup(lg, me, opp, start, n):
        windows.append(start)
        cat = SimpleNamespace(label="PTS", kind="count")
        o = SimpleNamespace(category=cat, mean_a=1.0, mean_b=1.0, p_win=0.5, p_tie=0.0, p_loss=0.5)
        odds = SimpleNamespace(categories=[o], expected_score=4.5, p_majority=0.5, swing=lambda: [])
        mw = SimpleNamespace(player_games=[])
        return odds, mw, mw, date(2026, 11, 16), date(2026, 11, 22)

    monkeypatch.setattr(svc, "_matchup", fake_matchup)
    return windows


def test_next_week_on_the_monday_before_rollover_is_week_five(monkeypatch):
    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    windows = _patch_next_week(monkeypatch, league)
    res = svc.next_week()
    assert (res["week"], res["this_week"]) == (5, 4)
    assert (res["start_et"], res["end_et"], res["days"]) == ("2026-11-16", "2026-11-22", 7)
    assert res["opponent"] == "Team Shohoku" and res["opponent_source"] == "Yahoo schedule"
    assert res["nba_games_by_team"] == {"MIA": 2, "BOS": 1, "LAL": 1}  # week 5's games, not week 4's 11-10 game
    assert "has not rolled over" in res["window_note"]
    assert windows == [date(2026, 11, 16)] and res["simulation"]["window_matches_week"]


def test_next_week_with_caller_opponent_still_uses_yahoos_window(monkeypatch):
    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    _patch_next_week(monkeypatch, league)
    res = svc.next_week("dimebag")
    assert res["opponent"] == "dimebag" and res["opponent_source"] == "caller"
    assert (res["week"], res["start_et"], res["end_et"]) == (5, "2026-11-16", "2026-11-22")


def test_next_week_mid_week_unchanged(monkeypatch):
    _yahoo_schedule(monkeypatch, today=date(2026, 11, 12))
    league = _league(4, date(2026, 11, 9), date(2026, 11, 15), DIMEBAG)
    assert svc._roll_week(league) is league
    _patch_next_week(monkeypatch, league)
    res = svc.next_week()
    assert (res["week"], res["this_week"], res["start_et"], res["end_et"]) == (5, 4, "2026-11-16", "2026-11-22")
    assert "window_note" not in res


def test_next_week_rolled_without_yahoo_schedule_says_so(monkeypatch):
    _yahoo_schedule(monkeypatch, schedule={})
    league = svc._roll_week(_league())
    _patch_next_week(monkeypatch, league)
    res = svc.next_week()
    assert res["week"] == 5 and (res["start_et"], res["end_et"]) == ("2026-11-16", "2026-11-22")
    assert res["opponent"] is None and "unavailable" in res["opponent_note"]
    assert "opponent is unknown" in res["window_note"]


def test_next_week_inconsistent_yahoo_bounds_are_reported_not_hidden(monkeypatch):
    bad = {5: _matchups(5, date(2026, 11, 9), date(2026, 11, 15), SHOHAKU)}  # overlaps the current week
    _yahoo_schedule(monkeypatch, schedule={**SCHEDULE, **bad})
    league = svc._roll_week(_league())
    _patch_next_week(monkeypatch, league)
    res = la.next_week()
    assert "don't follow" in res["window_note"] and res["start_et"] == "2026-11-16"


# ------------------------------------------------------------------ retro targets the finished week


def test_retro_default_week_is_the_finished_yahoo_week(monkeypatch):
    from fbm.gm.outcomes import retro_week

    assert retro_week(MONDAY) == (date(2026, 11, 2), date(2026, 11, 8), None)  # weekly_retro_data's default


@pytest.mark.parametrize(
    ("today", "start", "end", "repeat"),
    [
        (date(2026, 11, 30), date(2026, 11, 23), date(2026, 11, 29), False),  # the NBA Cup matchup starts
        (date(2026, 12, 7), date(2026, 11, 23), date(2026, 11, 29), True),  # its second half: nothing new ended
        (date(2026, 12, 9), date(2026, 11, 23), date(2026, 11, 29), True),
        (date(2026, 12, 14), date(2026, 11, 30), date(2026, 12, 13), False),  # the whole two-week matchup
        (date(2027, 2, 22), date(2027, 2, 8), date(2027, 2, 14), True),  # All-Star break
        (date(2027, 3, 1), date(2027, 2, 15), date(2027, 2, 28), False),
        (date(2026, 11, 18), date(2026, 11, 9), date(2026, 11, 15), False),  # mid-week: the last completed week
    ],
)
def test_retro_week_covers_merged_weeks_whole_and_never_half_played(today, start, end, repeat):
    from fbm.gm.outcomes import retro_week

    s, e, note = retro_week(today)
    assert (s, e) == (start, end) and e < today
    assert (note is not None and "already covered" in note) == repeat


def test_retro_week_for_a_given_monday_takes_its_whole_matchup():
    from fbm.gm.outcomes import retro_week

    assert retro_week(date(2026, 12, 20), date(2026, 12, 7))[:2] == (date(2026, 11, 30), date(2026, 12, 13))
    _s, _e, note = retro_week(date(2026, 12, 9), date(2026, 11, 30))
    assert note is not None and "still running" in note


def test_weekly_retro_tool_passes_the_merged_weeks_real_end(monkeypatch):
    from fbm.gm import decision_services

    seen = {}

    def fake_data(conn, pool, cats, week_start, today, punts, names=None, week_end=None):
        seen.update(start=week_start, end=week_end)
        return {"week_et": f"{week_start}..{week_end}"}

    monkeypatch.setattr(svc, "nba_today", lambda: date(2026, 12, 14))
    grading = SimpleNamespace(pool=None, cfg=SimpleNamespace(categories=[]), projections=pd.DataFrame({"name": []}))
    monkeypatch.setattr(svc, "_grading_ctx", lambda: grading)
    monkeypatch.setattr(decision_services, "sync_yahoo_results", lambda: {})
    monkeypatch.setattr(svc, "verify_moves", lambda sync_first=True: {})
    monkeypatch.setattr(svc, "_conn", lambda: None)
    monkeypatch.setattr(svc, "_strategy_punts", lambda punts: ())
    monkeypatch.setattr(svc.outcomes, "weekly_retro_data", fake_data)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    monkeypatch.setattr(svc, "_trade_offer_retro", lambda start, end: seen.update(offers=(start, end)) or {})
    monkeypatch.setattr(svc, "signal_grades_tool", lambda: {})
    out = svc.weekly_retro_data_tool()
    assert seen == {
        "start": date(2026, 11, 30),
        "end": date(2026, 12, 13),
        "offers": (date(2026, 11, 30), date(2026, 12, 13)),
    }
    assert out["week_et"] == "2026-11-30..2026-12-13" and out["week_complete"] and "week_note" not in out
    monkeypatch.setattr(svc, "nba_today", lambda: date(2026, 12, 7))
    out = svc.weekly_retro_data_tool()
    assert (seen["start"], seen["end"]) == (date(2026, 11, 23), date(2026, 11, 29)) and "already covered" in out[
        "week_note"
    ]


def test_yahoo_results_sync_includes_the_finished_week(monkeypatch):
    from fbm.gm import decision_services as ds

    _yahoo_schedule(monkeypatch)
    league = svc._roll_week(_league())
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    monkeypatch.setattr(svc, "_conn", lambda: None)
    monkeypatch.setattr(svc, "_yahoo_mapper", lambda lg: None)
    seen = {}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(svc, "_yahoo_client", Client)
    monkeypatch.setattr(
        "fbm.yahoo.lineups.sync_weeks", lambda conn, client, key, me, weeks, mapper: seen.update(weeks=weeks) or {}
    )
    ds.sync_yahoo_results()
    assert seen["weeks"] == [1, 2, 3]  # week 3 (Yahoo still calls it current) is graded, week 4 is not
