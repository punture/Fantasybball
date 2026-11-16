"""Late-week protection advisor: bench to protect close FG%, FT% or TO, or play every game for close counting cats.

Late in an H2H week some categories are settled while the ratio categories (FG%, FT%) or turnovers are close.
Benching a high-volume poor shooter or a turnover-prone player for the rest of the week can flip one of them; when
counting categories are close instead, every game counts and sitting anyone costs. This module scores those "sit"
options against this week's opponent with common random numbers (DrawBank): each option re-plans my daily lineups
(a bench player with a game may start instead) and re-totals only my side, so differences between options reflect
the benching, not Monte Carlo noise.

Options: each player with an unstarted game benched on one remaining ET day or from one day to the end of the week,
everyone with an unstarted game benched for the rest of the week (protect mode: bench players stay benched, so no
one comes in), and greedy pairs built on the best single benching.
Games that have tipped off are locked: they still count, but cannot be benched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from fbm.analytics.bank import DrawBank
from fbm.analytics.categories import Category
from fbm.analytics.lineup import LineupResult, as_of, optimize_day
from fbm.analytics.simulation import CategoryOdds, MatchupOdds, compare
from fbm.constants import C
from fbm.league.model import RosterPlayer

METRICS = ("expected_cats", "p_win_week")  # category leagues ("head") | one win per week ("headone")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SHOWN_DELTA = 0.02  # category odds changes smaller than this stay out of the reason text

PlayerDays = tuple[tuple[str, tuple[date, ...]], ...]  # (player_id, ET days benched)


@dataclass
class ProtectContext:
    """Everything the advisor needs; built by services from the league and analytics context."""

    players: Sequence[RosterPlayer]  # my roster (IL players stay in IL)
    days: Sequence[date]  # ET days still to simulate this week, in order
    actionable: Sequence[date]  # ET days whose lineup may still change (from the asked day on)
    playing: Mapping[date, set[str]]  # NBA teams playing each ET day
    starting_slots: Sequence[str]
    value: Mapping[str, float]  # lineup tie-break value (as in the daily lineup planner)
    bank: DrawBank
    opp_totals: np.ndarray  # the opponent's simulated week (banked + rest of the week), from the same bank
    categories: Sequence[Category]
    banked: Mapping[str, float] = field(default_factory=dict)  # my stats already banked this week
    skip: Mapping[date, set[str]] = field(default_factory=dict)  # ET day -> NBA teams whose games are banked
    locked: Mapping[date, set[str]] = field(default_factory=dict)  # ET day -> NBA teams whose games tipped off
    return_dates: Mapping[str, date] = field(default_factory=dict)  # Out player -> ESPN return date
    _lineups: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Option:
    kind: str  # "sit" (one player) | "pair" | "sit_all" (protect mode)
    sits: PlayerDays
    odds: MatchupOdds
    games: int  # my remaining games started under this option
    gain: float  # change in the objective (the plan's metric) vs the normal lineup
    gain_cats: float  # change in expected categories won
    gain_p_win: float  # change in P(win the week), a tied week counting half

    def by_day(self) -> dict[date, frozenset[str]]:
        return by_day(self.sits)

    @property
    def players(self) -> list[str]:
        return [pid for pid, _ in self.sits]


@dataclass
class ProtectPlan:
    metric: str
    min_gain: float
    baseline: MatchupOdds
    baseline_games: int
    candidate_days: dict[str, tuple[date, ...]]  # player -> actionable days he starts with an unstarted game
    best_by_player: list[Option]  # each candidate's best single benching, best first
    sit_all: Option | None  # protect mode: every player with an unstarted game sits, bench players included
    pair: Option | None  # best greedy pair (only when it clears the bar, plus the bar again over a single that does)
    actions: list[Option]  # recommended options (gain >= min_gain, none dominated by another), best first
    evaluated: int  # options simulated


def by_day(sits: PlayerDays) -> dict[date, frozenset[str]]:
    """(player, days) benchings -> ET day -> players benched that day."""
    out: dict[date, set[str]] = {}
    for pid, days in sits:
        for d in days:
            out.setdefault(d, set()).add(pid)
    return {d: frozenset(ids) for d, ids in out.items()}


def share(o: CategoryOdds) -> float:
    """Win odds for one category, a tie counting half."""
    return o.p_win + 0.5 * o.p_tie


def p_win_week(odds: MatchupOdds) -> float:
    """P(win the week) in a one-win-per-week league, a tied week counting half."""
    return odds.p_majority + 0.5 * max(0.0, 1.0 - odds.p_majority - odds.p_majority_loss)


def objective(odds: MatchupOdds, metric: str) -> float:
    return p_win_week(odds) if metric == "p_win_week" else odds.expected_score


def day_lineup(ctx: ProtectContext, d: date, sat: frozenset[str] = frozenset()) -> tuple[LineupResult, tuple[str, ...]]:
    """Optimal lineup for ET day `d` with the `sat` players benched, and who starts with a game.

    Players whose game tipped off keep their current slot (their game counts if they are in a starting slot)."""
    key = (d, sat)
    hit = ctx._lineups.get(key)
    if hit is None:
        roster = as_of(ctx.players, d, ctx.return_dates)
        teams = ctx.playing.get(d, set())
        tipped = ctx.locked.get(d, set())
        locked = {p.player_id for p in roster if p.nba_team in tipped}
        playing = {p.player_id for p in roster if p.nba_team in teams and p.player_id not in sat}
        res = optimize_day(roster, list(ctx.starting_slots), playing, ctx.value, locked)
        starting = set(ctx.starting_slots)
        started = tuple(pid for pid, slot in res.assignments.items() if slot in starting and pid in playing)
        hit = (res, started)
        ctx._lineups[key] = hit
    return hit


def evaluate(ctx: ProtectContext, sits: Mapping[date, frozenset[str]]) -> tuple[MatchupOdds, int]:
    """Matchup odds and my remaining started games with `sits` (ET day -> players benched that day)."""
    nba = {p.player_id: p.nba_team for p in ctx.players}
    games = [
        (pid, d)
        for d in ctx.days
        for pid in day_lineup(ctx, d, sits.get(d, frozenset()))[1]
        if nba[pid] not in ctx.skip.get(d, set())
    ]
    return compare(ctx.bank.total(games, ctx.banked), ctx.opp_totals, ctx.categories), len(games)


def candidate_days(ctx: ProtectContext, bench: bool = False) -> dict[str, tuple[date, ...]]:
    """Player -> actionable ET days on which he starts in the normal lineup with a game that has not tipped off.

    bench=True also lists players the normal lineup benches on a day they have such a game (protect mode sits them
    too, so nobody comes off the bench)."""
    nba = {p.player_id: p.nba_team for p in ctx.players}
    out: dict[str, list[date]] = {}
    for d in ctx.actionable:
        blocked = ctx.locked.get(d, set()) | ctx.skip.get(d, set())
        res, started = day_lineup(ctx, d)
        for pid in (*started, *res.benched_with_games) if bench else started:
            if nba[pid] not in blocked:
                out.setdefault(pid, []).append(d)
    return {pid: tuple(days) for pid, days in out.items()}


def windows(days: Sequence[date]) -> list[tuple[date, ...]]:
    """Benching windows for one player: each single game day, and each day through the end of the week."""
    singles = [(d,) for d in days]
    tails = [tuple(days[i:]) for i in range(len(days) - 1)]
    return singles + tails


def _rank(o: Option) -> tuple[float, float, int]:
    return (round(o.gain, 9), round(o.gain_cats, 9), o.games)  # ties: more expected cats, then fewer games benched


def _dominated(o: Option, others: Sequence[Option]) -> bool:
    """Another option gains at least as much while benching no more games, and is strictly better at one of them."""
    g = round(o.gain, 9)
    return any(
        x is not o and round(x.gain, 9) >= g and x.games >= o.games and (round(x.gain, 9) > g or x.games > o.games)
        for x in others
    )


def advise(
    ctx: ProtectContext,
    metric: str = "expected_cats",
    min_gain: float | None = None,
    pair_candidates: int | None = None,
) -> ProtectPlan:
    """Score every benching option against the normal lineup and return those worth making.

    metric: "expected_cats" (category leagues) or "p_win_week" (one win per week); both changes are reported."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}, got {metric!r}")
    if min_gain is None:
        min_gain = float(C("protect.min_gain_p_win" if metric == "p_win_week" else "protect.min_gain_cats"))
    k = int(C("protect.pair_candidates")) if pair_candidates is None else pair_candidates
    base_odds, base_games = evaluate(ctx, {})
    base_obj, base_p = objective(base_odds, metric), p_win_week(base_odds)
    evaluated = 0

    def option(kind: str, sits: PlayerDays) -> Option:
        nonlocal evaluated
        evaluated += 1
        odds, games = evaluate(ctx, by_day(sits))
        return Option(
            kind,
            sits,
            odds,
            games,
            objective(odds, metric) - base_obj,
            odds.expected_score - base_odds.expected_score,
            p_win_week(odds) - base_p,
        )

    cands = candidate_days(ctx)
    singles = {pid: [option("sit", ((pid, w),)) for w in windows(days)] for pid, days in cands.items()}
    best_by_player = sorted((max(opts, key=_rank) for opts in singles.values()), key=_rank, reverse=True)
    # protect mode sits every player with an unstarted game, bench players included (no one comes in instead)
    everyone = candidate_days(ctx, bench=True)
    sit_all = option("sit_all", tuple(everyone.items())) if cands and len(everyone) > 1 else None
    pair = None
    if best_by_player and best_by_player[0].gain > 0:
        first = best_by_player[0]
        others = [o.players[0] for o in best_by_player[1 : 1 + k]]
        tries = [option("pair", first.sits + s.sits) for pid in others for s in singles[pid]]
        best = max(tries, key=_rank, default=None)
        # the pair must clear the bar on its own, and add the bar again on top of a single benching that clears it
        bar = first.gain + min_gain if first.gain >= min_gain else min_gain
        if best is not None and best.gain >= bar:
            pair = best
    unique: dict[frozenset, Option] = {}  # a pair that benches everyone is protect mode: list it once, as such
    for o in (*best_by_player, sit_all, pair):
        if o is not None and o.gain >= min_gain:
            unique.setdefault(frozenset(o.by_day().items()), o)
    kept = list(unique.values())
    actions = sorted((o for o in kept if not _dominated(o, kept)), key=_rank, reverse=True)
    return ProtectPlan(
        metric, min_gain, base_odds, base_games, cands, best_by_player, sit_all, pair, actions, evaluated
    )


# ------------------------------------------------------------------ explaining an option


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.month:02d}-{d.day:02d}"


def window_text(days: Sequence[date], all_days: Sequence[date]) -> str:
    if len(days) == 1:
        return f"on {day_label(days[0])}"
    labels = ", ".join(WEEKDAYS[d.weekday()] for d in days)
    if tuple(days) == tuple(all_days):
        return f"for the rest of the week ({labels})"
    return f"from {day_label(days[0])} on ({labels})"


def category_changes(option: Option, baseline: MatchupOdds) -> list[tuple[Category, float, float]]:
    """(category, win odds before, after) for every category, ties counting half."""
    before = {o.category.key: share(o) for o in baseline.categories}
    return [(o.category, before[o.category.key], share(o)) for o in option.odds.categories]


def is_protect_category(cat: Category) -> bool:
    """Categories that benching can help: percentages and turnovers."""
    return cat.kind == "pct" or not cat.higher_is_better


def close_categories(odds: MatchupOdds, band: float | None = None) -> tuple[list[Category], list[Category]]:
    """Contested categories (win odds within `band` of 50%): (protect: FG%/FT%/TO, volume: counting)."""
    band = float(C("lineup.swing_band")) if band is None else band
    close = [o.category for o in odds.categories if abs(share(o) - 0.5) <= band]
    return [c for c in close if is_protect_category(c)], [c for c in close if not is_protect_category(c)]


def lineup_moves(ctx: ProtectContext, option: Option) -> dict[date, list[tuple[str, str, str]]]:
    """Per ET day, the lineup changes vs the normal lineup: (player_id, from slot, to slot)."""
    out: dict[date, list[tuple[str, str, str]]] = {}
    for d, sat in sorted(option.by_day().items()):
        base, base_started = day_lineup(ctx, d)
        res, started = day_lineup(ctx, d, sat)
        moves = [(pid, base.assignments[pid], "BN") for pid in sorted(sat) if base.assignments[pid] != "BN"]
        moves += [
            (pid, base.assignments.get(pid, "BN"), res.assignments[pid])
            for pid in started
            if res.assignments[pid] != base.assignments.get(pid)
        ]
        if moves:
            out[d] = moves
    return out


def replacements(ctx: ProtectContext, option: Option) -> dict[str, list[date]]:
    """Players who start (with a game) only because of the benching -> the ET days they come in."""
    out: dict[str, list[date]] = {}
    for d, sat in sorted(option.by_day().items()):
        base_started = set(day_lineup(ctx, d)[1])
        for pid in day_lineup(ctx, d, sat)[1]:
            if pid not in base_started:
                out.setdefault(pid, []).append(d)
    return out


def steps(ctx: ProtectContext, option: Option, names: Mapping[str, str]) -> list[str]:
    """Exact manual steps for the owner (FBM never changes the league): one line per group of identical days."""
    groups: dict[tuple[tuple[str, str, str], ...], list[date]] = {}
    for d, moves in lineup_moves(ctx, option).items():
        groups.setdefault(tuple(moves), []).append(d)
    lines = []
    for moves, days in groups.items():
        when = ", ".join(day_label(d) for d in days)
        what = "; ".join(f"move {names.get(pid, pid)} from {a} to {b}" for pid, a, b in moves)
        lines.append(f"In Yahoo, for {when} (ET): {what}.")
    return lines


def reason(ctx: ProtectContext, option: Option, plan: ProtectPlan, names: Mapping[str, str]) -> str:
    """One sentence: who sits when, which categories move, and the net effect."""
    if option.kind == "sit_all":
        who = "sitting everyone with an unstarted game for the rest of the week (protect mode)"
    else:
        who = "benching " + " and ".join(
            f"{names.get(pid, pid)} {window_text(days, plan.candidate_days.get(pid, days))}"
            for pid, days in option.sits
        )
    subs = replacements(ctx, option)
    if subs and option.kind != "sit_all":
        who += " (" + ", ".join(f"{names.get(pid, pid)} starts instead" for pid in subs) + ")"
    changes = category_changes(option, plan.baseline)
    ups = sorted((x for x in changes if x[2] - x[1] >= SHOWN_DELTA), key=lambda x: x[1] - x[2])
    downs = sorted((x for x in changes if x[1] - x[2] >= SHOWN_DELTA), key=lambda x: x[2] - x[1])
    effect = []
    if ups:
        effect.append(f"raises {_odds(ups, ' win odds')}")
    if downs:
        effect.append(f"costs {_odds(downs)}")
    text = f"{who} {' and '.join(effect)}" if effect else f"{who} moves no category by 2+ points"
    if ups and not downs:
        text += " without dropping any category 2+ points"
    before = p_win_week(plan.baseline)
    return (
        f"{text}; net {option.gain_cats:+.2f} expected cats, P(win week) {before:.0%}->{before + option.gain_p_win:.0%}"
    )


def _odds(items: list[tuple[Category, float, float]], first_suffix: str = "") -> str:
    """'FT% win odds 48%->71%, TO 50%->58%' for the three biggest changes."""
    return ", ".join(
        f"{c.label}{first_suffix if i == 0 else ''} {a:.0%}->{b:.0%}" for i, (c, a, b) in enumerate(items[:3])
    )


def _labels(cats: Sequence[Category]) -> str:
    verb = "is" if len(cats) == 1 else "are"
    return f"{', '.join(c.label for c in cats)} {verb}"


def gain_text(value: float, metric: str) -> str:
    return f"{value:.0%} P(win week)" if metric == "p_win_week" else f"{value:.2f} expected cats"


def guidance(
    ctx: ProtectContext, plan: ProtectPlan, names: Mapping[str, str], band: float | None = None
) -> dict[str, str | None]:
    """Plain-language verdict (`recommendation`) and what it means for streaming (`volume`, None if nothing)."""
    protect, volume = close_categories(plan.baseline, band)
    sit_all = plan.sit_all
    best = plan.best_by_player[0] if plan.best_by_player else None
    bar = gain_text(plan.min_gain, plan.metric)
    if plan.actions:
        rec = f"Bench: {reason(ctx, plan.actions[0], plan, names)}."
    elif volume and (sit_all is None or sit_all.gain_cats < 0):
        cost = f" (benching everyone would cost {-sit_all.gain_cats:.2f} expected cats)" if sit_all else ""
        rec = f"Play every game: {_labels(volume)} close, so volume matters{cost}."
    elif best is not None and best.gain > 0:
        rec = f"Keep the normal lineup: no benching gains {bar} or more (closest: {reason(ctx, best, plan, names)})."
    elif best is not None and best.gain_cats >= SHOWN_DELTA and plan.metric == "p_win_week":
        rec = (
            f"Keep the normal lineup: {reason(ctx, best, plan, names)}, but one win per week is at stake and "
            f"it doesn't move P(win week) by {plan.min_gain:.0%} or more."
        )
    elif protect:
        rec = f"Keep the normal lineup: {_labels(protect)} close, but no benching helps."
    else:
        rec = "Keep the normal lineup: no close FG%, FT% or TO category for benching to flip."
    vol = f"{_labels(volume)} close: more games help; see streaming_plan for adds." if volume else None
    if sit_all is not None:  # judged on the plan's objective (P(win week) in one-win-per-week leagues)
        partial = (o.gain for o in (*plan.best_by_player, plan.pair) if o and o.games > sit_all.games)
        if sit_all.gain > max(0.0, *partial):
            vol = (
                f"Benching everyone beats any partial benching ({sit_all.gain_cats:+.2f} expected cats, "
                f"{sit_all.gain_p_win:+.0%} P(win week)): extra games are more likely to hurt than help this week, "
                "so don't stream."
            )
    return {"recommendation": rec, "volume": vol}
