"""Monte Carlo category simulation for H2H weeks.

Each player-game is drawn by resampling the player's real recent box-score lines (keeps stat correlations such as
makes-with-attempts), re-centred on the projection table's per-game means. Each player's games in a simulated week
share one role/form multiplier; his weekly line is then rounded to whole numbers (unbiased stochastic rounding), so
categories tie at realistic rates and FG%/FT% stay ratios of whole makes and attempts.

Availability is a return-day process per player and simulation (`PlayerModel.return_cdf`): an injury status (GTD,
DTD, questionable, doubtful) sets his chance of playing his team's next game; once he sits, he is back for each later
game with the status's recovery rate and then plays the rest (absences are streaky, not a coin flip per game). An
Out player misses every game before his ESPN return date and plays from it like a game-time decision; without a
return date he misses a geometric number of games (mean availability.out_default_games). Back-to-back rest risk is
an independent draw per game. Team weekly totals are simulated once per team, so any pair of teams can be compared
cheaply (matchups, league-wide category profiles).
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from fbm.analytics.categories import Category
from fbm.constants import C

STATS: tuple[str, ...] = ("pts", "reb", "ast", "stl", "blk", "tov", "tpm", "fgm", "fga", "ftm", "fta")
COL = {s: i for i, s in enumerate(STATS)}

# Yahoo / ESPN injury statuses -> availability class (unknown statuses count as healthy).
STATUS_CLASS = {
    "": "healthy",
    "ACTIVE": "healthy",
    "GTD": "gtd",
    "DTD": "dtd",
    "DAY-TO-DAY": "dtd",
    "Q": "questionable",
    "QUESTIONABLE": "questionable",
    "D": "doubtful",
    "DOUBTFUL": "doubtful",
    "O": "out",
    "OUT": "out",
    "INJ": "out",
    "SUSP": "suspended",
    "SUSPENSION": "suspended",
    "OFS": "season",
    "OUT FOR SEASON": "season",
    "NA": "season",
}
NEXT_GAME_PROB = {
    "gtd": "sim.play_prob.gtd",
    "dtd": "sim.play_prob.dtd",
    "questionable": "sim.play_prob.questionable",
    "doubtful": "sim.play_prob.doubtful",
}
RECOVERY = {
    "gtd": "sim.recovery.short",
    "dtd": "sim.recovery.short",
    "questionable": "sim.recovery.short",
    "doubtful": "sim.recovery.doubtful",
}
GAMES_MISSED = {"out": "availability.out_default_games", "suspended": "availability.susp_default_games"}


def _status_class(status: str | None) -> str:
    return STATUS_CLASS.get((status or "").strip().upper(), "healthy")


def play_probability(status: str | None) -> float:
    """Chance a player with this injury status plays his team's next game."""
    cls = _status_class(status)
    if cls in NEXT_GAME_PROB:
        return float(C(NEXT_GAME_PROB[cls]))
    return 1.0 if cls == "healthy" else 0.0


def recovery_rate(status: str | None) -> float | None:
    """Return-day process: chance that a player with this status who has sat his team's last game(s) is back for the
    next one. Out/suspended (no return date): 1 / expected games missed, a geometric absence with mean
    availability.out_default_games / availability.susp_default_games. Out for the season: 0. Healthy: None."""
    cls = _status_class(status)
    if cls in RECOVERY:
        return float(C(RECOVERY[cls]))
    if cls in GAMES_MISSED:
        return 1.0 / max(1.0, float(C(GAMES_MISSED[cls])))
    return 0.0 if cls == "season" else None


@dataclass
class PlayerModel:
    player_id: str
    samples: np.ndarray  # (k, len(STATS)) per-game lines
    play_prob: float = 1.0  # chance he plays his team's next game (every game when no return-day process applies)
    synthetic: bool = False
    form_sd: float = 0.0  # sd of the weekly multiplier shared by all of this player's games in a simulated week
    rest_prob: float = 0.0  # extra chance of sitting a back-to-back leg (load management)
    b2b_dates: frozenset = frozenset()  # ET dates that are one leg of a back-to-back for the player's NBA team
    return_date: date | None = None  # Out player's ESPN return date: misses every game before it
    return_play_prob: float = 0.0  # chance he plays his first game on/after the return date
    recovery: float | None = None  # return-day process: chance he is back for each later game once he sits
    as_of: date | None = None  # ET day his status applies from: his team's first game on/after it is "the next game"
    schedule: tuple[date, ...] = ()  # his NBA team's game days, sorted (counts games for the return-day process)

    @property
    def mean(self) -> np.ndarray:
        return self.samples.mean(axis=0)

    def games_between(self, start: date, day: date) -> float:
        """His team's games on ET days in [start, day): counted from `schedule`, else estimated from the average
        schedule (week.avg_games_per_week)."""
        if day <= start:
            return 0.0
        if self.schedule:
            return float(bisect_left(self.schedule, day) - bisect_left(self.schedule, start))
        return (day - start).days * float(C("week.avg_games_per_week")) / 7.0

    def return_cdf(self, day: object) -> float | None:
        """P(back from injury for his team's game on ET `day`) under the return-day process:
        1 - (1 - p0) * (1 - recovery) ** k, where p0 is his first-game probability (`play_prob` from `as_of`, or
        `return_play_prob` from the return date) and k his team's games between that start and `day`.

        None when the process does not apply (no recovery rate, no start day, or `day` is not a date): each game is
        then an independent draw at the play probability."""
        if self.recovery is None or not isinstance(day, date):
            return None
        start, first = self.as_of, self.play_prob
        if self.return_date is not None:
            if day < self.return_date:
                return 0.0
            start, first = max(start or self.return_date, self.return_date), self.return_play_prob
        if start is None:
            return None
        return 1.0 - (1.0 - first) * (1.0 - self.recovery) ** self.games_between(start, day)

    def status_prob(self, day: object = None) -> float:
        """Chance he is available (injury/status only) for the game on `day`."""
        cdf = self.return_cdf(day)
        if cdf is not None:
            return cdf
        if self.return_date is not None and isinstance(day, date) and day >= self.return_date:
            return self.return_play_prob
        return self.play_prob

    def game_play_prob(self, day: object = None, rest: bool = True) -> float:
        """Chance he plays the game on `day`: injury/status availability; back-to-back legs carry the player's rest
        risk (unless `rest=False`: injury/status availability only)."""
        p = self.status_prob(day)
        if rest and self.rest_prob > 0 and day is not None and day in self.b2b_dates:
            return p * (1.0 - self.rest_prob)
        return p

    def play_mask(
        self, day: object, rng: np.random.Generator, n: int, back: np.ndarray, scale: float = 1.0
    ) -> np.ndarray | None:
        """In which of `n` simulations he plays the game on `day` (None = all of them).

        `back`: his return-day draws (one uniform per simulation, shared by all of his games): under the return-day
        process he is available when `back < return_cdf(day)`, so once back he stays back. Otherwise availability is
        an independent draw. Back-to-back rest and `scale` (e.g. a start share) are independent draws per game."""
        cdf = self.return_cdf(day)
        independent = (1.0 if cdf is not None else self.status_prob(day)) * scale
        if self.rest_prob > 0 and day is not None and day in self.b2b_dates:
            independent *= 1.0 - self.rest_prob
        keep = None if cdf is None or cdf >= 1.0 else back < cdf
        if independent < 1.0:
            draw = rng.random(n) < independent
            keep = draw if keep is None else keep & draw
        return keep

    def set_availability(self, prob: float, start: date | None = None) -> None:
        """Override injury availability: he plays his first game on/after ET `start` (default: his status day) with
        probability `prob`, misses every game before it, and then recovers like a game-time decision."""
        if start is None:
            self.play_prob, self.return_date, self.return_play_prob = float(prob), None, 0.0
        else:
            self.play_prob, self.return_date, self.return_play_prob = 0.0, start, float(prob)
        self.recovery = recovery_rate("GTD")


def form_multiplier(rng: np.random.Generator, n: int, sd: float) -> np.ndarray:
    """Mean-one lognormal weekly multiplier with (approximately) standard deviation `sd`."""
    if sd <= 0:
        return np.ones(n)
    s = float(np.sqrt(np.log1p(sd**2)))
    return np.exp(rng.normal(-0.5 * s**2, s, n))


# Makes reuse their attempts' rounding draw, so rounding keeps 3PM <= FGM <= FGA and FTM <= FTA.
_LINKED_ROUNDING = ((COL["fgm"], COL["fga"]), (COL["tpm"], COL["fga"]), (COL["ftm"], COL["fta"]))


def rounding_draws(rng: np.random.Generator, n: int, dtype: type = np.float64) -> np.ndarray:
    """Uniforms in [0, 1) for `round_counts` (n x len(STATS)), makes linked to their attempts."""
    u = rng.random((n, len(STATS)), dtype=dtype)
    for made, att in _LINKED_ROUNDING:
        u[:, made] = u[:, att]
    return u


def round_counts(lines: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Unbiased stochastic rounding of simulated stat lines to whole numbers: floor(x + u), u ~ U[0, 1), has mean x.
    Real stat lines are whole numbers, so this keeps ties at realistic rates after the form multiplier and
    projection anchoring make counts continuous."""
    return np.floor(lines + u)


def anchor_samples(samples: np.ndarray, target: pd.Series, weight: float, lo: float, hi: float) -> np.ndarray:
    """Rescale each stat column so its mean moves toward the projection's per-game mean (`weight` 1 = all the way).

    Column-wise scaling keeps each game's shape (and makes/attempts pairing); makes and attempts are scaled
    separately so shooting percentages follow the projection too. A game's makes are then capped at its attempts
    (and 3PM at FGM): scaling makes up can push a perfect-shooting game past its attempts. The capped excess moves
    onto the games with room (`_cap_makes`), so the makes mean (and FG%/FT%) still follows the projection."""
    if weight <= 0 or len(samples) == 0:
        return samples
    out = samples.astype(float, copy=True)
    means = out.mean(axis=0)
    for s, i in COL.items():
        t = target.get(s)
        if t is None or not np.isfinite(t) or means[i] <= 0:
            continue
        goal = (1.0 - weight) * means[i] + weight * float(t)
        out[:, i] *= float(np.clip(goal / means[i], lo, hi))
    for made, att in (("fgm", "fga"), ("ftm", "fta"), ("tpm", "fgm")):
        out[:, COL[made]] = _cap_makes(out[:, COL[made]], out[:, COL[att]])
    return out


def _cap_makes(made: np.ndarray, att: np.ndarray, rounds: int = 25) -> np.ndarray:
    """Cap each game's makes at its attempts, keeping the column's total where the attempts leave room: the excess
    is spread over the games below their cap in proportion to their makes (a plain cap would pull the mean, and so
    the shooting percentage, below the projection: about half of a +2-point FT% adjustment is lost to perfect
    free-throw games)."""
    goal = float(made.sum())
    out = np.minimum(made, att)
    for _ in range(rounds):
        short = goal - float(out.sum())
        room = (out < att) & (out > 0)
        base = float(out[room].sum())
        if short <= 1e-9 * max(goal, 1.0) or base <= 0:
            break
        out[room] = np.minimum(out[room] * (1.0 + short / base), att[room])
    return out


def _per_game(row: pd.Series, stat: str) -> float:
    try:
        value = float(row.get(stat, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return value if np.isfinite(value) and value > 0 else 0.0


def _synthetic_games(row: pd.Series, n: int, rng: np.random.Generator) -> np.ndarray:
    """Draw plausible per-game lines from per-game means (thin history).

    A per-game volume factor (gamma, mean 1, sd sim.synthetic_volume_cv; minutes and usage) scales every rate, so
    attempts are overdispersed and a game's stats move together. Makes are binomial out of attempts, 3PM a binomial
    share of FGM, and points follow from the makes: PTS = 2 FGM + 3PM + FTM."""
    cv = float(C("sim.synthetic_volume_cv"))
    volume = rng.gamma(1.0 / cv**2, cv**2, n) if cv > 0 else np.ones(n)
    out = np.zeros((n, len(STATS)))
    for s in ("reb", "ast", "stl", "blk", "tov"):
        out[:, COL[s]] = rng.poisson(_per_game(row, s) * volume)
    fga_mean, fta_mean = _per_game(row, "fga"), _per_game(row, "fta")
    fg_pct = min(_per_game(row, "fgm") / fga_mean, 1.0) if fga_mean else 0.45
    ft_pct = min(_per_game(row, "ftm") / fta_mean, 1.0) if fta_mean else 0.75
    fgm_mean = fga_mean * fg_pct
    three_share = min(_per_game(row, "tpm") / fgm_mean, 1.0) if fgm_mean else 0.0
    fga = rng.poisson(fga_mean * volume)
    fgm = rng.binomial(fga, fg_pct)
    tpm = rng.binomial(fgm, three_share)
    fta = rng.poisson(fta_mean * volume)
    ftm = rng.binomial(fta, ft_pct)
    out[:, COL["fga"]], out[:, COL["fgm"]], out[:, COL["tpm"]] = fga, fgm, tpm
    out[:, COL["fta"]], out[:, COL["ftm"]] = fta, ftm
    out[:, COL["pts"]] = 2 * fgm + tpm + ftm
    return out


def build_player_models(
    conn: sqlite3.Connection,
    player_ids: Iterable[str],
    seasons: Sequence[int],
    projections: pd.DataFrame | None = None,
    statuses: Mapping[str, str] | None = None,
    max_games: int = 40,
    min_games: int = 8,
    seed: int | None = 7,
    play_probs: Mapping[str, float] | None = None,
    rest_probs: Mapping[str, float] | None = None,
    b2b_dates: Mapping[str, frozenset] | None = None,
    return_dates: Mapping[str, date] | None = None,
    anchor: float | None = None,
    as_of: date | None = None,
    schedules: Mapping[str, Iterable[date]] | None = None,
) -> dict[str, PlayerModel]:
    """Recent real games (most recent seasons first), re-centred on the projection's per-game means
    (`anchor`, default `sim.projection_anchor`; HT mode passes 1.0 so the means equal Hashtag's and the game logs
    only supply the spread); synthetic from projections when history is thin.

    `play_probs` overrides the status-derived play probability per player (e.g. rest-of-season availability); each
    game is then an independent draw. `rest_probs` / `b2b_dates` add back-to-back rest risk on those dates (this-week
    sims only). Status-based availability follows the return-day process from `as_of` (the ET day the statuses
    apply from), counting games on `schedules` (player -> his NBA team's game days). `return_dates` (ESPN): a player
    whose status rules him out misses every game before that ET date and plays from it like a game-time decision."""
    ids = list(dict.fromkeys(player_ids))
    rng = np.random.default_rng(seed)
    statuses = statuses or {}
    rest_probs = rest_probs or {}
    b2b_dates = b2b_dates or {}
    return_dates = return_dates or {}
    schedules = schedules or {}
    lo, hi = float(C("sim.anchor_scale_min")), float(C("sim.anchor_scale_max"))
    if anchor is None:
        anchor = float(C("sim.projection_anchor"))
    else:  # explicit anchor (HT mode): means follow the projection exactly, no scale clipping
        anchor, lo, hi = float(anchor), 0.0, float("inf")
    form_sd = float(C("sim.form_sd"))
    models: dict[str, PlayerModel] = {}
    if not ids:
        return models
    placeholders = ",".join("?" * len(ids))
    season_list = ",".join(str(int(s)) for s in seasons)
    lines = pd.read_sql_query(
        f"SELECT espn_id, et_date, {', '.join(STATS)} FROM box_lines "
        f"WHERE played = 1 AND espn_id IN ({placeholders}) AND season IN ({season_list}) ORDER BY et_date DESC",
        conn,
        params=ids,
    )
    grouped = {pid: g for pid, g in lines.groupby("espn_id")}
    for pid in ids:
        recent = grouped.get(pid)
        samples = recent[list(STATS)].head(max_games).to_numpy(dtype=float) if recent is not None else np.empty((0, 11))
        synthetic = False
        if len(samples) < min_games and projections is not None and pid in projections.index:
            samples = _synthetic_games(projections.loc[pid], max_games, rng)
            synthetic = True
        elif len(samples) and projections is not None and pid in projections.index:
            samples = anchor_samples(samples, projections.loc[pid], anchor, lo, hi)
        if len(samples) == 0:
            samples = np.zeros((1, len(STATS)))
            synthetic = True
        overridden = play_probs is not None and pid in play_probs
        status = statuses.get(pid, "")
        prob = play_probs[pid] if overridden else play_probability(status)
        returns = return_dates.get(pid) if not overridden and prob <= 0 else None
        if overridden:
            recovery = None
        else:
            recovery = recovery_rate("GTD") if returns is not None else recovery_rate(status)
        models[pid] = PlayerModel(
            pid,
            samples,
            float(prob),
            synthetic,
            form_sd=form_sd,
            rest_prob=float(rest_probs.get(pid, 0.0)),
            b2b_dates=frozenset(b2b_dates.get(pid, frozenset())),
            return_date=returns,
            return_play_prob=play_probability("GTD") if returns is not None else 0.0,
            recovery=recovery,
            as_of=as_of,
            schedule=tuple(sorted(set(schedules.get(pid, ())))),
        )
    return models


@dataclass
class TeamWeek:
    team_key: str
    player_games: list[str]  # one entry per remaining started game
    banked: dict[str, float] = field(default_factory=dict)  # stats already accumulated this week
    play_scale: dict[str, float] = field(default_factory=dict)  # extra per-player multipliers (e.g. start share)
    game_dates: list | None = None  # ET date of each entry in player_games (enables back-to-back rest)


def simulate_totals(
    week: TeamWeek, models: Mapping[str, PlayerModel], n_sims: int, rng: np.random.Generator
) -> np.ndarray:
    """Week totals per simulation (n_sims x len(STATS)): banked stats plus each player's simulated games, his week
    scaled by his form multiplier and rounded to whole numbers."""
    totals = np.zeros((n_sims, len(STATS)))
    for s, v in week.banked.items():
        if s in COL:
            totals[:, COL[s]] += v
    dates = week.game_dates if week.game_dates is not None else [None] * len(week.player_games)
    per_player: dict[str, np.ndarray] = {}
    back: dict[str, np.ndarray] = {}
    for pid, day in zip(week.player_games, dates, strict=True):
        model = models.get(pid)
        if model is None:
            continue
        scale = week.play_scale.get(pid, 1.0)
        if model.game_play_prob(day) * scale <= 0:
            continue
        if pid not in back:
            back[pid] = rng.random(n_sims)
        draws = model.samples[rng.integers(0, len(model.samples), n_sims)]
        keep = model.play_mask(day, rng, n_sims, back[pid], scale)
        if keep is not None:
            draws = draws * keep[:, None]
        per_player[pid] = draws if pid not in per_player else per_player[pid] + draws
    for pid, week_lines in per_player.items():
        sd = models[pid].form_sd
        scaled = week_lines * form_multiplier(rng, n_sims, sd)[:, None] if sd > 0 else week_lines
        totals += round_counts(scaled, rounding_draws(rng, n_sims))
    return totals


def category_values(totals: np.ndarray, cat: Category) -> np.ndarray:
    if cat.kind == "pct":
        made, att = totals[:, COL[cat.made]], totals[:, COL[cat.att]]
        return np.divide(made, att, out=np.zeros_like(made), where=att > 0)
    return totals[:, COL[cat.stat]]


@dataclass(frozen=True)
class CategoryOdds:
    category: Category
    p_win: float
    p_tie: float
    p_loss: float
    mean_a: float
    mean_b: float


@dataclass(frozen=True)
class MatchupOdds:
    categories: tuple[CategoryOdds, ...]
    expected_score: float  # expected category wins, ties count 0.5
    p_majority: float  # P(win more categories than lose)
    cats_won_hist: tuple[float, ...]  # P(exactly k categories won), k = 0..n
    p_majority_loss: float = 0.0  # P(lose more categories than win)

    @property
    def p_week_tie(self) -> float:
        """P(as many categories won as lost): a tied week under Yahoo 'headone' (most categories) scoring."""
        return max(0.0, 1.0 - self.p_majority - self.p_majority_loss)

    def swing(self, low: float = 0.25, high: float = 0.75) -> list[CategoryOdds]:
        return [c for c in self.categories if low <= c.p_win + 0.5 * c.p_tie <= high]


def _mean_where(values: np.ndarray, mask: np.ndarray) -> float:
    return float(values[mask].mean()) if mask.any() else 0.0


def compare(totals_a: np.ndarray, totals_b: np.ndarray, categories: Sequence[Category]) -> MatchupOdds:
    """Category odds of side a vs side b over paired simulations, scored like Yahoo H2H.

    Equal values are a tie (a T in Yahoo's category record; expected_score counts it half, never splits it into a
    win). Percentages compare exact ratios of whole makes/attempts (Yahoo breaks rounded-display ties on the unrounded
    figures). A side with no attempts has no percentage (Yahoo shows '-'): it loses to any percentage and ties
    another '-'. `p_majority` = P(more categories won than lost): the week win under 'headone' scoring, where an
    equal count is a tied week (`p_week_tie`)."""
    n = len(totals_a)
    wins = np.zeros(n)
    losses = np.zeros(n)
    odds = []
    for cat in categories:
        a, b = category_values(totals_a, cat), category_values(totals_b, cat)
        diff = a - b if cat.higher_is_better else b - a
        if cat.kind == "pct":
            has_a, has_b = totals_a[:, COL[cat.att]] > 0, totals_b[:, COL[cat.att]] > 0
            diff = np.where(has_a & has_b, diff, has_a.astype(float) - has_b.astype(float))
            mean_a, mean_b = _mean_where(a, has_a), _mean_where(b, has_b)
        else:
            mean_a, mean_b = float(a.mean()), float(b.mean())
        tie = np.abs(diff) <= 1e-9
        win = (diff > 0) & ~tie
        loss = (diff < 0) & ~tie
        wins += win
        losses += loss
        odds.append(CategoryOdds(cat, float(win.mean()), float(tie.mean()), float(loss.mean()), mean_a, mean_b))
    hist = np.bincount(wins.astype(int), minlength=len(categories) + 1) / n
    expected = float(sum(o.p_win + 0.5 * o.p_tie for o in odds))
    return MatchupOdds(
        tuple(odds),
        expected,
        float((wins > losses).mean()),
        tuple(float(x) for x in hist),
        float((losses > wins).mean()),
    )


def simulate_matchup(
    me: TeamWeek,
    opp: TeamWeek,
    models: Mapping[str, PlayerModel],
    categories: Sequence[Category],
    n_sims: int = 10000,
    seed: int | None = None,
) -> MatchupOdds:
    rng = np.random.default_rng(seed)
    return compare(simulate_totals(me, models, n_sims, rng), simulate_totals(opp, models, n_sims, rng), categories)
