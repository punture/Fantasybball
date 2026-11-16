"""Simulator realism: whole-number stat lines (ties), Yahoo-style category scoring, synthetic games, anchor caps."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from fbm.analytics.bank import DrawBank
from fbm.analytics.categories import FT_PCT, NINE_CAT
from fbm.analytics.simulation import (
    COL,
    STATS,
    PlayerModel,
    TeamWeek,
    _synthetic_games,
    anchor_samples,
    compare,
    round_counts,
    rounding_draws,
    simulate_totals,
)


def line(**kw) -> np.ndarray:
    row = np.zeros(len(STATS))
    for k, v in kw.items():
        row[COL[k]] = v
    return row


def totals(**kw) -> np.ndarray:
    return np.array([line(**kw)])


def test_round_counts_is_unbiased_and_keeps_makes_within_attempts():
    rng = np.random.default_rng(0)
    fga = rng.uniform(0, 30, 50_000)
    fgm = fga * rng.uniform(0, 1, fga.size)
    tpm = fgm * rng.uniform(0, 1, fga.size)
    fta = rng.uniform(0, 10, fga.size)
    lines = np.zeros((fga.size, len(STATS)))
    lines[:, COL["stl"]] = rng.uniform(0, 3, fga.size)
    lines[:, COL["fga"]], lines[:, COL["fgm"]], lines[:, COL["tpm"]] = fga, fgm, tpm
    lines[:, COL["fta"]], lines[:, COL["ftm"]] = fta, fta * rng.uniform(0, 1, fga.size)
    out = round_counts(lines, rounding_draws(np.random.default_rng(1), fga.size))
    assert (out == np.floor(out)).all()
    assert np.allclose(out.mean(axis=0), lines.mean(axis=0), atol=0.01)  # E[floor(x + u)] = x
    assert (np.abs(out - lines) < 1).all()
    assert (out[:, COL["tpm"]] <= out[:, COL["fgm"]]).all() and (out[:, COL["fgm"]] <= out[:, COL["fga"]]).all()
    assert (out[:, COL["ftm"]] <= out[:, COL["fta"]]).all()


def _steals_team(form_sd: float, scale: float) -> dict[str, PlayerModel]:
    games = np.array([line(stl=s, fgm=4, fga=9, ftm=2, fta=3) for s in (0, 1, 1, 2, 3)]) * scale
    return {f"p{i}": PlayerModel(f"p{i}", games, form_sd=form_sd) for i in range(8)}


def _stl_tie_rate(models: dict[str, PlayerModel]) -> tuple[float, np.ndarray]:
    ids = list(models)
    week_a = TeamWeek("a", [pid for pid in ids[:4] for _ in range(4)])
    week_b = TeamWeek("b", [pid for pid in ids[4:] for _ in range(4)])
    rng = np.random.default_rng(5)
    a = simulate_totals(week_a, models, 40_000, rng)
    b = simulate_totals(week_b, models, 40_000, rng)
    by = {o.category.key: o for o in compare(a, b, NINE_CAT).categories}
    return by["stl"].p_tie, a


def test_form_shock_and_anchoring_keep_realistic_tie_rates_and_unbiased_means():
    reference, _ = _stl_tie_rate(_steals_team(form_sd=0.0, scale=1.0))  # raw whole-number games
    tie, a = _stl_tie_rate(_steals_team(form_sd=0.09, scale=1.07))  # weekly form shock + anchored (fractional) lines
    assert reference > 0.05
    assert tie == pytest.approx(reference, rel=0.25)  # was ~0 before rounding: continuous totals never tie
    assert (a == np.floor(a)).all()  # FG%/FT% are ratios of whole makes/attempts
    assert a[:, COL["stl"]].mean() == pytest.approx(16 * 1.4 * 1.07, rel=0.01)  # 4 players x 4 games, unbiased


def test_bank_totals_are_whole_numbers_and_additive_across_players():
    bank = DrawBank(_steals_team(form_sd=0.09, scale=1.07), n_sims=3000, seed=9)
    ga = [("p0", d) for d in range(4)]
    gb = [("p1", d) for d in range(3)]
    both = bank.total(ga + gb)
    assert (both == np.floor(both)).all()
    # each player rounds his own week with his own draws: candidates that share players share their lines exactly
    assert np.array_equal(both, bank.total(ga) + bank.total(gb))
    assert np.array_equal(bank.total(ga, {"stl": 3.0}), bank.total(ga) + totals(stl=3)[0])


def test_tied_categories_are_ties_and_headone_week_ties():
    # a wins PTS/REB/AST/STL, loses BLK/TO/3PM/FG%, ties FT%: 4-4-1 is a tied week under 'headone'
    a = totals(pts=100, reb=50, ast=30, stl=10, blk=2, tov=20, tpm=5, fgm=40, fga=100, ftm=15, fta=20)
    b = totals(pts=90, reb=40, ast=20, stl=8, blk=5, tov=10, tpm=9, fgm=45, fga=100, ftm=30, fta=40)
    odds = compare(a, b, NINE_CAT)
    by = {o.category.key: o for o in odds.categories}
    assert by["ft_pct"].p_tie == 1.0 and by["ft_pct"].p_win == 0.0  # 15/20 == 30/40 exactly
    assert odds.p_majority == 0.0 and odds.p_majority_loss == 0.0 and odds.p_week_tie == 1.0
    assert odds.expected_score == pytest.approx(4.5)  # head: ties count half
    assert odds.cats_won_hist[4] == 1.0  # a tie is not a category won
    b2 = b.copy()
    b2[0, COL["tov"]] = 25  # a now also wins TO: 5-3-1 wins the week
    assert compare(a, b2, NINE_CAT).p_majority == 1.0


def test_percentage_with_no_attempts_has_no_value():
    none = totals(ftm=0, fta=0)
    bricks = totals(ftm=0, fta=4)
    odds = compare(none, bricks, [FT_PCT]).categories[0]
    assert (odds.p_win, odds.p_tie, odds.p_loss) == (0.0, 0.0, 1.0)  # '-' loses even to 0/4
    assert compare(none, none.copy(), [FT_PCT]).categories[0].p_tie == 1.0
    lower_is_better = replace(FT_PCT, key="ft_miss", higher_is_better=False)
    assert compare(none, bricks, [lower_is_better]).categories[0].p_loss == 1.0  # no free win for '-'
    mixed = np.vstack([totals(ftm=8, fta=10), totals(ftm=0, fta=0)])
    assert compare(mixed, mixed.copy(), [FT_PCT]).categories[0].mean_a == pytest.approx(0.8)  # '-' not averaged as 0


ROW = pd.Series(
    {"pts": 20.0, "reb": 5.0, "ast": 4.0, "stl": 1.0, "blk": 0.5, "tov": 2.0, "tpm": 2.0, "fgm": 7.5, "fga": 16.0}
    | {"ftm": 3.0, "fta": 3.8}
)


def test_synthetic_games_derive_points_from_makes():
    g = _synthetic_games(ROW, 40_000, np.random.default_rng(3))
    c = COL
    assert np.array_equal(g[:, c["pts"]], 2 * g[:, c["fgm"]] + g[:, c["tpm"]] + g[:, c["ftm"]])
    assert (g[:, c["tpm"]] <= g[:, c["fgm"]]).all() and (g[:, c["fgm"]] <= g[:, c["fga"]]).all()
    assert (g[:, c["ftm"]] <= g[:, c["fta"]]).all()
    for s in ("fga", "fgm", "tpm", "ftm", "fta", "reb", "ast", "stl"):
        assert g[:, c[s]].mean() == pytest.approx(ROW[s], rel=0.03), s
    assert g[:, c["pts"]].mean() == pytest.approx(2 * 7.5 + 2.0 + 3.0, rel=0.02)
    assert np.corrcoef(g[:, c["pts"]], g[:, c["fgm"]])[0, 1] > 0.9  # was ~0: points drawn apart from makes
    assert g[:, c["fga"]].var() > 1.5 * g[:, c["fga"]].mean()  # attempts overdispersed (Poisson: var = mean)


def test_synthetic_games_tolerate_missing_columns():
    g = _synthetic_games(pd.Series({"pts": 4.0, "reb": np.nan, "fga": None}), 100, np.random.default_rng(0))
    assert np.isfinite(g).all() and (g[:, COL["reb"]] == 0).all() and (g[:, COL["fga"]] == 0).all()


def test_anchor_caps_makes_at_attempts():
    samples = np.array([line(fgm=2, fga=2, tpm=2, ftm=1, fta=1), line(fgm=2, fga=10, tpm=0, ftm=1, fta=4)])
    target = pd.Series({"fgm": 4.0, "fga": 6.0, "tpm": 1.5, "ftm": 2.0, "fta": 2.5})
    out = anchor_samples(samples, target, 1.0, 0.5, 2.0)
    assert (out[:, COL["fgm"]] <= out[:, COL["fga"]]).all() and (out[:, COL["ftm"]] <= out[:, COL["fta"]]).all()
    assert (out[:, COL["tpm"]] <= out[:, COL["fgm"]]).all()
    assert out[0, COL["fgm"]] == 2.0  # a 2-for-2 game scaled up stays 2-for-2, not 4-for-2
