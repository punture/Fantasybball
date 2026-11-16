"""Late-week protection advisor (fbm.analytics.protect): benching to protect close FG%/FT%/TO, offline."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from fbm.analytics import protect
from fbm.analytics.bank import DrawBank
from fbm.analytics.categories import NINE_CAT
from fbm.analytics.simulation import COL, STATS, PlayerModel
from fbm.league.model import RosterPlayer

THU, FRI, SAT, SUN = date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 21), date(2026, 11, 22)
DAYS = [THU, FRI, SAT, SUN]
SLOTS = ["C", "UTIL"]


def line(**kw: float) -> np.ndarray:
    row = np.zeros(len(STATS))
    for k, v in kw.items():
        row[COL[k]] = v
    return row


def lines(*rows: dict[str, float]) -> np.ndarray:
    return np.array([line(**r) for r in rows])


# A rim-running big who can't shoot free throws (and turns it over), a guard who can, and a sixth man who can too.
BIG = lines(
    dict(pts=14, reb=12, blk=2, tov=3, fgm=6, fga=9, ftm=2, fta=6),
    dict(pts=10, reb=10, blk=1, tov=2, fgm=4, fga=7, ftm=2, fta=5),
    dict(pts=16, reb=13, blk=3, tov=3, fgm=7, fga=10, ftm=2, fta=7),
    dict(pts=12, reb=11, blk=2, tov=2, fgm=5, fga=8, ftm=2, fta=6),
)
GUARD = lines(
    dict(pts=22, reb=4, ast=7, stl=1, tov=2, tpm=3, fgm=7, fga=16, ftm=5, fta=5),
    dict(pts=18, reb=3, ast=6, stl=2, tov=3, tpm=2, fgm=6, fga=15, ftm=4, fta=5),
    dict(pts=26, reb=5, ast=8, stl=1, tov=2, tpm=4, fgm=8, fga=17, ftm=6, fta=7),
    dict(pts=20, reb=4, ast=5, stl=1, tov=2, tpm=3, fgm=7, fga=15, ftm=3, fta=4),
)
SHOOTER = lines(
    dict(pts=9, reb=2, ast=1, tov=1, tpm=1, fgm=3, fga=7, ftm=2, fta=2),
    dict(pts=7, reb=1, ast=2, tov=0, tpm=1, fgm=2, fga=6, ftm=2, fta=2),
    dict(pts=11, reb=3, ast=1, tov=1, tpm=2, fgm=4, fga=8, ftm=1, fta=1),
)
OPP = lines(
    dict(pts=20, reb=6, ast=4, stl=1, blk=1, tov=2, tpm=2, fgm=7, fga=15, ftm=4, fta=5),
    dict(pts=16, reb=5, ast=3, stl=1, blk=0, tov=2, tpm=1, fgm=6, fga=14, ftm=3, fta=4),
    dict(pts=24, reb=7, ast=5, stl=2, blk=1, tov=3, tpm=3, fgm=8, fga=17, ftm=6, fta=8),
    dict(pts=18, reb=6, ast=4, stl=1, blk=1, tov=2, tpm=2, fgm=7, fga=15, ftm=3, fta=4),
)

# Banked Mon-Wed: I lead every counting category, FG% and TO by a mile; FT% is close (75.0% vs 74.5%), but the
# big's free throws (about 33%) would sink mine.
ME_BANKED = dict(pts=700, reb=330, ast=170, stl=45, blk=40, tov=40, tpm=75, fgm=260, fga=500, ftm=120, fta=160)
OPP_BANKED = dict(pts=420, reb=180, ast=90, stl=20, blk=12, tov=75, tpm=35, fgm=150, fga=360, ftm=102, fta=137)


def rp(pid: str, team: str, slot: str, eligible: tuple[str, ...] = ("C", "UTIL")) -> RosterPlayer:
    return RosterPlayer(pid, pid.title(), team, eligible, slot)


def build(
    roster: list[RosterPlayer],
    models: dict[str, np.ndarray],
    opp_games: list[tuple[str, date]],
    me_banked: dict[str, float] = ME_BANKED,
    opp_banked: dict[str, float] = OPP_BANKED,
    playing: dict[date, set[str]] | None = None,
    days: list[date] = DAYS,
    locked: dict[date, set[str]] | None = None,
    skip: dict[date, set[str]] | None = None,
    value: dict[str, float] | None = None,
    slots: list[str] = SLOTS,
    n: int = 3000,
) -> protect.ProtectContext:
    bank = DrawBank({pid: PlayerModel(pid, s) for pid, s in models.items()}, n_sims=n, seed=11)
    teams = {p.nba_team for p in roster}
    return protect.ProtectContext(
        players=roster,
        days=days,
        actionable=days,
        playing=playing if playing is not None else {d: set(teams) for d in days},
        starting_slots=slots,
        value=value or {p.player_id: 1.0 for p in roster},
        bank=bank,
        opp_totals=bank.total(opp_games, opp_banked),
        categories=NINE_CAT,
        banked=me_banked,
        skip=skip or {},
        locked=locked or {},
    )


def opp_games(days: list[date] = DAYS) -> list[tuple[str, date]]:
    return [(pid, d) for d in days for pid in ("o1", "o2")]


def big_and_guard(**kw) -> protect.ProtectContext:
    roster = [rp("big", "AAA", "C"), rp("guard", "BBB", "UTIL")]
    models = {"big": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    return build(roster, models, opp_games(kw.pop("opp_days", DAYS)), **kw)


def odds(plan: protect.ProtectPlan, option: protect.Option, label: str) -> tuple[float, float]:
    return next((a, b) for c, a, b in protect.category_changes(option, plan.baseline) if c.label == label)


def test_benching_a_bad_free_throw_big_flips_ft_without_losing_anything():
    ctx = big_and_guard()
    plan = protect.advise(ctx, "expected_cats")
    best = plan.actions[0]
    assert best.kind == "sit" and best.sits == (("big", tuple(DAYS)),)  # the whole rest of the week
    before, after = odds(plan, best, "FT%")
    assert before < 0.4 and after > 0.6
    assert all(b >= a - 0.005 for _, a, b in protect.category_changes(best, plan.baseline))  # nothing else drops
    assert best.gain_cats >= plan.min_gain and best.games == plan.baseline_games - 4
    names = {"big": "Big Man", "guard": "Guard"}
    text = protect.reason(ctx, best, plan, names)
    assert text.startswith("benching Big Man for the rest of the week (Thu, Fri, Sat, Sun) raises FT% win odds")
    assert "without dropping any category" in text and "net +" in text and "P(win week)" in text
    assert protect.steps(ctx, best, names) == [
        "In Yahoo, for Thu 11-19, Fri 11-20, Sat 11-21, Sun 11-22 (ET): move Big Man from C to BN."
    ]
    assert protect.guidance(ctx, plan, names)["recommendation"].startswith("Bench: benching Big Man")
    assert all(o.players != ["guard"] for o in plan.actions)  # benching the good shooter only hurts FT%
    assert plan.sit_all is not None and plan.sit_all.gain > 0 and plan.actions == [best]  # protect mode: dominated


def test_nothing_recommended_when_counting_categories_are_close_and_ratios_are_settled():
    me = dict(pts=500, reb=200, ast=110, stl=30, blk=18, tov=30, tpm=50, fgm=230, fga=440, ftm=140, fta=160)
    opp = dict(pts=510, reb=205, ast=112, stl=31, blk=19, tov=70, tpm=52, fgm=150, fga=400, ftm=90, fta=150)
    ctx = big_and_guard(me_banked=me, opp_banked=opp)
    plan = protect.advise(ctx, "expected_cats")
    assert plan.actions == [] and plan.pair is None
    assert plan.sit_all is not None and plan.sit_all.gain_cats < -1.0  # every game matters
    _, volume = protect.close_categories(plan.baseline)
    assert volume and all(c.kind == "count" and c.higher_is_better for c in volume)
    guide = protect.guidance(ctx, plan, {"big": "Big", "guard": "Guard"})
    assert guide["recommendation"].startswith(f"Play every game: {volume[0].label}")
    assert "streaming_plan" in (guide["volume"] or "")


def test_one_win_per_week_objective_ignores_a_category_that_cannot_change_the_week():
    ctx = big_and_guard()  # I win 8 categories for sure: FT% can't change the week's result
    head = protect.advise(ctx, "expected_cats")
    headone = protect.advise(ctx, "p_win_week")
    assert head.actions and head.actions[0].gain_cats > 0.2
    assert headone.actions == [] and protect.p_win_week(headone.baseline) == pytest.approx(1.0)
    assert headone.min_gain == pytest.approx(0.02) and head.min_gain == pytest.approx(0.05)
    rec = protect.guidance(ctx, headone, {"big": "Big", "guard": "Guard"})["recommendation"]
    assert rec.startswith("Keep the normal lineup: benching Big") and "doesn't move P(win week) by 2%" in rec
    with pytest.raises(ValueError):
        protect.advise(ctx, "points")


def test_one_win_per_week_objective_benches_when_ft_decides_the_week():
    # 4 categories won (REB, BLK, FG%, TO), 4 lost (PTS, AST, STL, 3PM): FT% decides the week.
    me = dict(pts=300, reb=330, ast=60, stl=10, blk=40, tov=40, tpm=15, fgm=260, fga=500, ftm=120, fta=160)
    opp = dict(pts=700, reb=180, ast=200, stl=50, blk=12, tov=75, tpm=80, fgm=150, fga=360, ftm=104, fta=137)
    ctx = big_and_guard(me_banked=me, opp_banked=opp)
    plan = protect.advise(ctx, "p_win_week")
    best = plan.actions[0]
    assert best.players == ["big"] and best.gain == pytest.approx(best.gain_p_win)
    assert best.gain_p_win > 0.2 and protect.p_win_week(plan.baseline) < 0.4


def test_tip_off_locks_are_respected():
    # Big's Thursday game has tipped off (not banked yet): it still counts, but only Fri-Sun can be benched.
    ctx = big_and_guard(locked={THU: {"AAA"}})
    plan = protect.advise(ctx, "expected_cats")
    assert plan.candidate_days["big"] == (FRI, SAT, SUN) and plan.candidate_days["guard"] == tuple(DAYS)
    best = plan.actions[0]
    assert best.sits == (("big", (FRI, SAT, SUN)),)
    assert plan.baseline_games == 8 and best.games == 5  # his locked Thursday game is still played
    assert all(THU not in days for o in plan.best_by_player for pid, days in o.sits if pid == "big")
    steps = protect.steps(ctx, best, {"big": "Big"})
    assert steps == ["In Yahoo, for Fri 11-20, Sat 11-21, Sun 11-22 (ET): move Big from C to BN."]


def test_a_player_whose_only_game_has_tipped_off_is_not_a_candidate():
    playing = {THU: {"AAA", "BBB"}, FRI: {"BBB"}, SAT: {"BBB"}, SUN: {"BBB"}}
    ctx = big_and_guard(locked={THU: {"AAA"}}, playing=playing)
    plan = protect.advise(ctx, "expected_cats")
    assert "big" not in plan.candidate_days and all("big" not in o.players for o in plan.best_by_player)
    assert plan.sit_all is None  # one candidate left: protect mode is the same as benching him


def test_a_later_day_keeps_the_normal_lineup_until_then():
    ctx = big_and_guard()
    ctx.actionable = [SAT, SUN]  # asked about Saturday: Thu and Fri are played with the normal lineup
    plan = protect.advise(ctx, "expected_cats")
    assert plan.baseline_games == 8 and plan.candidate_days == {"big": (SAT, SUN), "guard": (SAT, SUN)}
    big = next(o for o in plan.best_by_player if o.players == ["big"])
    assert big.sits == (("big", (SAT, SUN)),) and big.games == 6 and big.gain > 0  # Thu-Fri still count


def test_banked_games_are_neither_simulated_nor_benchable():
    ctx = big_and_guard(skip={THU: {"AAA", "BBB"}})
    plan = protect.advise(ctx, "expected_cats")
    assert plan.baseline_games == 6 and plan.candidate_days["big"] == (FRI, SAT, SUN)


def test_a_bench_player_with_a_game_starts_instead_and_the_steps_say_so():
    roster = [rp("big", "AAA", "C"), rp("guard", "BBB", "UTIL"), rp("shooter", "CCC", "BN")]
    models = {"big": BIG, "guard": GUARD, "shooter": SHOOTER, "o1": OPP, "o2": OPP}
    value = {"big": 2.0, "guard": 3.0, "shooter": -1.0}
    ctx = build(roster, models, opp_games(), value=value)
    plan = protect.advise(ctx, "expected_cats")
    best = plan.actions[0]
    assert best.players == ["big"] and best.games == plan.baseline_games  # a swap, not a lost game
    names = {"big": "Big", "guard": "Guard", "shooter": "Shooter"}
    assert protect.replacements(ctx, best) == {"shooter": DAYS}
    assert protect.steps(ctx, best, names) == [
        "In Yahoo, for Thu 11-19, Fri 11-20, Sat 11-21, Sun 11-22 (ET): move Big from C to BN; "
        "move Shooter from BN to C."
    ]
    assert "(Shooter starts instead)" in protect.reason(ctx, best, plan, names)


def test_greedy_pair_when_one_benching_is_not_enough():
    roster = [rp("big", "AAA", "C"), rp("big2", "DDD", "C"), rp("guard", "BBB", "UTIL")]
    models = {"big": BIG, "big2": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    # FT%: the opponent ends near 73.5%; with the guard I end near 76% without the bigs, 72% with one of them.
    me = dict(ME_BANKED, ftm=150, fta=200)
    opp = dict(OPP_BANKED, ftm=144, fta=198)
    ctx = build(roster, models, opp_games(), me_banked=me, opp_banked=opp, slots=["C", "C", "UTIL"])
    plan = protect.advise(ctx, "expected_cats")
    single = plan.best_by_player[0]
    assert plan.pair is not None and plan.actions[0] is plan.pair
    assert plan.pair.sits == (("big", tuple(DAYS)), ("big2", tuple(DAYS))) or plan.pair.sits == (
        ("big2", tuple(DAYS)),
        ("big", tuple(DAYS)),
    )
    assert plan.pair.gain - single.gain >= plan.min_gain
    assert odds(plan, plan.pair, "FT%")[1] > 0.6 > odds(plan, single, "FT%")[1]
    # protect mode wins FT% too, but benches the guard for nothing: dominated by the pair, so not listed
    assert plan.sit_all is not None and plan.sit_all.games < plan.pair.games and plan.sit_all not in plan.actions
    text = protect.reason(ctx, plan.pair, plan, {"big": "Big", "big2": "Big Two"})
    assert " and Big" in text and "for the rest of the week" in text


def test_protect_mode_on_sunday_when_ahead_in_close_ratios():
    # Sunday: counting categories are decided either way; I lead FG%, FT% and TO narrowly. Sitting everyone locks
    # them in, and extra games could only hurt.
    me = dict(pts=700, reb=150, ast=170, stl=20, blk=40, tov=60, tpm=30, fgm=250, fga=520, ftm=120, fta=155)
    opp = dict(pts=420, reb=300, ast=90, stl=45, blk=12, tov=62, tpm=70, fgm=245, fga=520, ftm=118, fta=155)
    roster = [rp("big", "AAA", "C"), rp("guard", "BBB", "UTIL")]
    models = {"big": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    ctx = build(roster, models, [], me_banked=me, opp_banked=opp, days=[SUN])
    plan = protect.advise(ctx, "expected_cats")
    assert plan.sit_all is not None and plan.sit_all.gain_cats >= plan.min_gain
    assert plan.actions[0].kind == "sit_all" and set(plan.actions[0].players) == {"big", "guard"}
    names = {"big": "Big", "guard": "Guard"}
    assert protect.reason(ctx, plan.sit_all, plan, names).startswith("sitting everyone with an unstarted game")
    assert protect.steps(ctx, plan.sit_all, names) == [
        "In Yahoo, for Sun 11-22 (ET): move Big from C to BN; move Guard from UTIL to BN."
    ]
    assert "don't stream" in (protect.guidance(ctx, plan, names)["volume"] or "")


def test_protect_mode_sits_bench_players_with_games_too():
    # Sitting "everyone" must not bring a bench player with a game into the lineup.
    me = dict(pts=700, reb=150, ast=170, stl=20, blk=40, tov=60, tpm=30, fgm=250, fga=520, ftm=120, fta=155)
    opp = dict(pts=420, reb=300, ast=90, stl=45, blk=12, tov=62, tpm=70, fgm=245, fga=520, ftm=118, fta=155)
    roster = [rp("big", "AAA", "C"), rp("guard", "BBB", "UTIL"), rp("shooter", "CCC", "BN")]
    models = {"big": BIG, "guard": GUARD, "shooter": SHOOTER, "o1": OPP, "o2": OPP}
    value = {"big": 2.0, "guard": 3.0, "shooter": -1.0}
    ctx = build(roster, models, [], me_banked=me, opp_banked=opp, days=[SUN], value=value)
    plan = protect.advise(ctx, "expected_cats")
    assert plan.baseline_games == 2 and set(plan.candidate_days) == {"big", "guard"}
    sit_all = plan.sit_all
    assert sit_all is not None and sit_all.games == 0 and set(sit_all.players) == {"big", "guard", "shooter"}
    names = {"big": "Big", "guard": "Guard", "shooter": "Shooter"}
    assert protect.replacements(ctx, sit_all) == {}
    assert protect.steps(ctx, sit_all, names) == [
        "In Yahoo, for Sun 11-22 (ET): move Big from C to BN; move Guard from UTIL to BN."
    ]


def test_a_pair_that_clears_the_bar_is_kept_when_no_single_benching_does():
    roster = [rp("big", "AAA", "C"), rp("big2", "DDD", "C"), rp("guard", "BBB", "UTIL")]
    models = {"big": BIG, "big2": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    me = dict(ME_BANKED, ftm=150, fta=200)
    opp = dict(OPP_BANKED, ftm=144, fta=198)
    ctx = build(roster, models, opp_games(), me_banked=me, opp_banked=opp, slots=["C", "C", "UTIL"])
    loose = protect.advise(ctx, "expected_cats", min_gain=0.0)
    single, pair = loose.best_by_player[0], loose.pair
    assert pair is not None and 0 < single.gain < pair.gain
    # a bar above the single benching and above what the second sit adds, but below the pair's total gain
    bar = (max(single.gain, pair.gain - single.gain) + pair.gain) / 2
    plan = protect.advise(ctx, "expected_cats", min_gain=bar)
    assert plan.pair is not None and plan.pair.sits == pair.sits and plan.actions[0] is plan.pair
    assert all(o.kind != "sit" for o in plan.actions)  # the single benching alone stays below the bar


def test_dont_stream_advice_follows_the_objective():
    # Sunday: five counting categories are locked in my favor, so a one-win league's week is already won; FG%, FT%
    # and TO are close. Benching everyone gains categories, but not the week.
    me = dict(pts=700, reb=330, ast=170, stl=45, blk=40, tov=60, tpm=30, fgm=250, fga=520, ftm=120, fta=155)
    opp = dict(pts=420, reb=180, ast=90, stl=20, blk=12, tov=62, tpm=70, fgm=245, fga=520, ftm=118, fta=155)
    roster = [rp("big", "AAA", "C"), rp("guard", "BBB", "UTIL")]
    models = {"big": BIG, "guard": GUARD, "o1": OPP, "o2": OPP}
    ctx = build(roster, models, [], me_banked=me, opp_banked=opp, days=[SUN])
    names = {"big": "Big", "guard": "Guard"}
    head = protect.advise(ctx, "expected_cats")
    assert "don't stream" in (protect.guidance(ctx, head, names)["volume"] or "")
    headone = protect.advise(ctx, "p_win_week")
    assert protect.p_win_week(headone.baseline) == pytest.approx(1.0) and headone.sit_all.gain_cats > 0.5
    assert "don't stream" not in (protect.guidance(ctx, headone, names)["volume"] or "")


def test_windows_and_labels():
    assert protect.windows([THU, SAT, SUN]) == [(THU,), (SAT,), (SUN,), (THU, SAT, SUN), (SAT, SUN)]
    assert protect.windows([SUN]) == [(SUN,)]
    assert protect.day_label(THU) == "Thu 11-19"
    assert protect.window_text((SAT,), (THU, SAT)) == "on Sat 11-21"
    assert protect.window_text((THU, SAT), (THU, SAT)) == "for the rest of the week (Thu, Sat)"
    assert protect.window_text((SAT, SUN), (THU, SAT, SUN)) == "from Sat 11-21 on (Sat, Sun)"
