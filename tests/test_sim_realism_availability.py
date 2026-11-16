"""Return-day availability process: injury statuses mostly concern the next game, absences are streaky."""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from fbm.analytics.bank import DrawBank
from fbm.analytics.context import AnalyticsContext
from fbm.analytics.simulation import (
    COL,
    STATS,
    PlayerModel,
    TeamWeek,
    build_player_models,
    play_probability,
    recovery_rate,
    simulate_totals,
)
from fbm.analytics.valuation import PoolStats, ValuationConfig
from fbm.constants import C
from fbm.store import db

MON = date(2026, 11, 16)
TUE, WED, FRI, SUN = (MON + timedelta(days=d) for d in (1, 2, 4, 6))
SCHEDULE = (MON, WED, FRI, SUN)
ONE_POINT = np.array([[1.0] + [0.0] * (len(STATS) - 1)])  # 1 pt per game played: totals count games


def model(status: str = "", **kw) -> PlayerModel:
    kw = {"as_of": MON, "schedule": SCHEDULE, "recovery": recovery_rate(status)} | kw
    return PlayerModel("x", ONE_POINT, play_probability(status), **kw)


def games_played(m: PlayerModel, days=SCHEDULE, n: int = 40_000) -> np.ndarray:
    week = TeamWeek("t", ["x"] * len(days), game_dates=list(days))
    return simulate_totals(week, {"x": m}, n, np.random.default_rng(4))[:, COL["pts"]]


def test_status_mapping_covers_questionable_and_doubtful_abbreviations():
    assert play_probability("Q") == play_probability("questionable") == C("sim.play_prob.questionable")
    assert play_probability("d") == play_probability("DOUBTFUL") == C("sim.play_prob.doubtful")
    assert play_probability("GTD") == C("sim.play_prob.gtd") and play_probability("Day-To-Day") == C(
        "sim.play_prob.dtd"
    )
    assert play_probability("O") == play_probability("INJ") == play_probability("OFS") == play_probability("NA") == 0.0
    assert play_probability(None) == play_probability("ACTIVE") == play_probability("weird") == 1.0
    assert recovery_rate("GTD") == recovery_rate("Q") == C("sim.recovery.short")
    assert recovery_rate("D") == C("sim.recovery.doubtful")
    assert recovery_rate("O") == pytest.approx(1 / C("availability.out_default_games"))
    assert recovery_rate("SUSP") == pytest.approx(1 / C("availability.susp_default_games"))
    assert recovery_rate("OFS") == 0.0 and recovery_rate("") is None


def test_status_applies_to_the_next_game_then_decays():
    p, r = C("sim.play_prob.gtd"), C("sim.recovery.short")
    m = model("GTD")
    assert m.return_cdf(MON) == pytest.approx(p)
    assert m.return_cdf(TUE) == m.return_cdf(WED) == pytest.approx(1 - (1 - p) * (1 - r))  # one team game later
    assert m.return_cdf(SUN) == pytest.approx(1 - (1 - p) * (1 - r) ** 3)
    assert model("GTD", as_of=TUE).return_cdf(WED) == pytest.approx(p)  # no game Tuesday: GTD means Wednesday's
    healthy = model("")
    assert healthy.return_cdf(MON) is None and healthy.game_play_prob(SUN) == 1.0


def test_absences_are_streaky_and_marginals_match():
    m = model("Q")
    played = games_played(m)
    p, r = C("sim.play_prob.questionable"), C("sim.recovery.short")
    expected = sum(1 - (1 - p) * (1 - r) ** k for k in range(4))
    assert played.mean() == pytest.approx(expected, rel=0.01)
    assert (played == 0).mean() == pytest.approx((1 - p) * (1 - r) ** 3, abs=0.005)
    bank = DrawBank({"x": m}, n_sims=20_000, seed=2)
    plays = [bank.game("x", d)[:, COL["pts"]] > 0 for d in SCHEDULE]
    for earlier, later in zip(plays, plays[1:], strict=False):
        assert (earlier <= later).all()  # once back, he stays back
    for d, mask in zip(SCHEDULE, plays, strict=True):
        assert mask.mean() == pytest.approx(m.return_cdf(d), abs=0.012)


def test_out_without_return_date_misses_a_geometric_number_of_games():
    h = 1 / C("availability.out_default_games")
    m = model("O")
    assert m.return_cdf(MON) == 0.0 and m.return_cdf(WED) == pytest.approx(h)
    played = games_played(m)
    assert played.mean() == pytest.approx(sum(1 - (1 - h) ** k for k in range(4)), rel=0.03)  # not 0: may return
    assert (played == 4).mean() == 0.0  # never plays the next game


def test_out_with_return_date_plays_from_it_like_a_game_time_decision():
    p, r = C("sim.play_prob.gtd"), C("sim.recovery.short")
    m = model("O", return_date=FRI, return_play_prob=p, recovery=r)
    assert m.return_cdf(WED) == 0.0 and m.return_cdf(FRI) == pytest.approx(p)
    assert m.return_cdf(SUN) == pytest.approx(1 - (1 - p) * (1 - r))
    assert games_played(m).mean() == pytest.approx(p + 1 - (1 - p) * (1 - r), rel=0.02)
    stale = model("O", return_date=MON - timedelta(days=3), return_play_prob=p, recovery=r)
    assert stale.return_cdf(MON) == pytest.approx(p)  # past return date, still listed out: from today


def test_rest_risk_stays_an_independent_draw():
    m = model("GTD", rest_prob=0.3, b2b_dates=frozenset({FRI}))
    assert m.game_play_prob(FRI) == pytest.approx(m.return_cdf(FRI) * 0.7)
    assert m.game_play_prob(FRI, rest=False) == pytest.approx(m.return_cdf(FRI))
    played = games_played(m)
    assert played.mean() == pytest.approx(sum(m.game_play_prob(d) for d in SCHEDULE), rel=0.01)


def test_without_start_day_or_dates_each_game_is_independent():
    p = C("sim.play_prob.gtd")
    legacy = model("GTD", as_of=None)
    assert legacy.return_cdf(MON) is None and legacy.game_play_prob(SUN) == p
    assert model("GTD").return_cdf(("generic", 0)) is None  # schedule-neutral weeks
    played = games_played(legacy)
    assert played.mean() == pytest.approx(4 * p, rel=0.01)
    assert (played == 0).mean() == pytest.approx((1 - p) ** 4, abs=0.005)


def test_games_counted_from_average_schedule_without_team_schedule():
    m = model("GTD", schedule=())
    per_day = C("week.avg_games_per_week") / 7
    assert m.games_between(MON, WED) == pytest.approx(2 * per_day) and m.games_between(WED, MON) == 0.0
    assert model("GTD").games_between(MON, SUN) == 3.0


def test_set_availability_override():
    m = model("O", return_date=FRI, return_play_prob=0.6, recovery=0.5)
    m.set_availability(0.9, WED)
    assert m.return_cdf(MON) == 0.0 and m.return_cdf(WED) == pytest.approx(0.9)
    assert m.recovery == C("sim.recovery.short")
    m.set_availability(0.5)
    assert m.return_date is None and m.return_cdf(MON) == pytest.approx(0.5)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _games(conn, team: str, days) -> None:
    for i, d in enumerate(days):
        conn.execute(
            "INSERT INTO games(event_id, season, season_type, start_utc, et_date, home, away, state, completed, "
            "neutral, time_valid, updated_at) VALUES (?, 2027, 2, ?, ?, ?, 'OPP', 'pre', 0, 0, 1, '')",
            (f"{team}{i}", f"{d}T00:00:00Z", d.isoformat(), team),
        )


def test_build_player_models_sets_the_return_day_process(conn):
    statuses = {"q": "Q", "o": "O", "back": "OUT", "season": "OFS", "ros": "O"}
    models = build_player_models(
        conn,
        [*statuses, "healthy"],
        [2027],
        statuses=statuses,
        play_probs={"ros": 0.4},
        return_dates={"back": FRI, "q": SUN},
        as_of=MON,
        schedules={pid: [SUN, MON, WED, FRI] for pid in statuses},
    )
    assert models["q"].recovery == C("sim.recovery.short") and models["q"].return_date is None  # Q: next game
    assert models["q"].schedule == SCHEDULE and models["q"].as_of == MON
    assert models["o"].recovery == pytest.approx(recovery_rate("O"))
    back = models["back"]
    assert back.return_date == FRI and back.recovery == C("sim.recovery.short")
    assert back.return_play_prob == C("sim.play_prob.gtd")
    assert models["season"].game_play_prob(SUN) == 0.0
    assert models["ros"].recovery is None and models["ros"].game_play_prob(SUN) == 0.4  # overrides stay per game
    assert models["healthy"].recovery is None and models["healthy"].game_play_prob(MON) == 1.0


def test_context_week_models_use_team_schedule_and_status_day(conn):
    _games(conn, "PHI", [MON, WED, FRI, SUN])
    conn.execute(
        "INSERT INTO injuries_current(espn_id, status, fantasy_status, fetched_at) VALUES (?, ?, ?, '')",
        ("p", "Day-To-Day", "GTD"),
    )
    conn.commit()
    projections = pd.DataFrame({"team": ["PHI"]}, index=["p"])
    ctx = AnalyticsContext(
        conn, 2027, 2026, projections, pd.DataFrame(), PoolStats({}, {}, {}, {}, {}, []), ValuationConfig()
    )
    m = ctx.models(["p"], as_of=TUE)["p"]
    assert m.schedule == SCHEDULE and m.as_of == TUE
    assert m.return_cdf(WED) == pytest.approx(C("sim.play_prob.gtd"))  # his next game is Wednesday
    season = ctx.models(["p"], horizon="season")["p"]
    assert season.recovery is None


@pytest.mark.parametrize(
    ("status", "ret", "effective", "override", "expect"),
    [
        # GTD/Q/D: the status model built from today is kept (not restarted at the effective day as a GTD)
        ("D", None, "2026-10-21", None, {"play_prob": "status", "return_date": None, "recovery": None}),
        # Out with an ESPN return date before the effective day: his return process starts at the return date
        ("O", "2026-10-21", "2026-10-22", None, {"return_date": date(2026, 10, 21), "recovery": "short"}),
        # explicit play_prob: his first game from the effective day at that probability
        ("O", "2026-10-21", "2026-10-22", 0.9, {"return_date": date(2026, 10, 22), "return_play_prob": 0.9}),
    ],
)
def test_il_activation_availability(monkeypatch, conn, status, ret, effective, override, expect):
    from fbm.gm import il
    from tests.test_il_waivers_dupes import il_world

    seen: dict[str, PlayerModel] = {}
    bank = il.DrawBank

    def capture(models, **kw):
        seen.update(models)
        return bank(models, **kw)

    monkeypatch.setattr(il, "DrawBank", capture)
    il_world(monkeypatch, conn, status=status, return_date=ret)
    il.evaluate_il_activation("I", "D", effective=effective, play_prob=override, n=200)
    m = seen["I"]
    named = {"status": play_probability(status), "short": C("sim.recovery.short")}
    for attr, value in expect.items():
        value = named.get(value, value) if isinstance(value, str) else value
        assert getattr(m, attr) == (pytest.approx(value) if isinstance(value, float) else value), attr
    if expect.get("recovery") == "short":
        assert m.return_play_prob == pytest.approx(C("sim.play_prob.gtd"))
