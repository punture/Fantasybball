"""Weekly streaming planner.

Greedy search over add/drop moves (with effective ET days) within the remaining weekly adds. The objective follows
the league's scoring: weighted expected categories won against this week's opponent (H2H Categories: each category
counts; weights = the contested-category weights the chooser also uses, punts 0), or P(win the matchup) (Most
Categories: one win per week). Every candidate is scored by re-planning the daily lineups for the rest of the week and
re-simulating with common random numbers (DrawBank).

League rules respected: moves made now take effect from `first_change_day` (Daily - Tomorrow leagues: tomorrow, and
today's lineup stands as set); an added player gets no credit for a game that already tipped (`tipped`); IL players
able to play (healthy, or from their ESPN return date) are activated once an active roster spot is open, and the plan
reports those still waiting for one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

import numpy as np

from fbm.analytics.bank import DrawBank
from fbm.analytics.categories import Category
from fbm.analytics.lineup import activate_ready, as_of, optimize_day
from fbm.analytics.simulation import MatchupOdds, compare
from fbm.analytics.week import p_win_week
from fbm.constants import C
from fbm.league.model import IL_SLOTS, RosterPlayer

OBJECTIVE_CATS = "cats"  # weighted expected categories won (H2H Categories)
OBJECTIVE_P_WIN = "p_win"  # P(win the matchup) (H2H Most Categories: one win per week)


class InvalidPlan(ValueError):
    pass


@dataclass(frozen=True)
class Move:
    day: date  # first ET day the added player is on the roster (the dropped player is gone from this day)
    add: str
    drop: str | None


@dataclass(frozen=True)
class PlannedMove:
    move: Move
    gain: float  # increase in the planner's objective from this move (given earlier moves)
    ros_cost: float  # rest-of-season value given up (>= 0)
    add_games: int  # add's games on/after move.day within the window (games already tipped excluded)
    drop_games_lost: int  # drop's games on/after move.day within the window
    expected_cats_gain: float = 0.0  # change in expected categories won (non-punted, unweighted)
    p_win_gain: float = 0.0  # change in P(win the matchup)


@dataclass
class StreamPlan:
    days: list[date]
    baseline: MatchupOdds
    final: MatchupOdds
    baseline_score: float
    final_score: float
    starts_before: int
    starts_after: int
    moves: list[PlannedMove] = field(default_factory=list)
    adds_available: int = 0
    candidates_considered: int = 0
    objective: str = OBJECTIVE_CATS
    waiting_returns: dict[str, date] = field(default_factory=dict)  # IL player -> first day ready with no spot


@dataclass(frozen=True)
class DayRoster:
    ids: tuple[str, ...]  # everyone on my roster that day
    held: frozenset[str]  # players in an IL slot that day (they don't play)
    waiting: tuple[str, ...] = ()  # IL players able to play with no active spot (a drop is needed)


@dataclass
class StreamContext:
    """Everything the planner needs; built by services from the league + analytics context."""

    base: list[RosterPlayer]  # my current roster (including IL players)
    pool: Mapping[str, RosterPlayer]  # RosterPlayer for every base player and free-agent candidate
    days: list[date]
    playing: Mapping[date, set[str]]  # NBA team abbreviations playing on each ET day
    starting_slots: list[str]
    capacity: int  # active (non-IL) roster spots
    value: Mapping[str, float]  # rest-of-season value (punt-aware) used for lineup tie-breaks and ROS cost
    bank: DrawBank
    opp_totals: np.ndarray
    categories: Sequence[Category]
    weights: Mapping[str, float]  # category key -> objective weight (0 for punts)
    banked: Mapping[str, float] = field(default_factory=dict)
    skip: Mapping[date, set[str]] = field(default_factory=dict)  # ET day -> NBA teams whose games are banked
    add_not_before: Mapping[str, date] = field(default_factory=dict)  # player on waivers -> first day he can be added
    return_dates: Mapping[str, date] = field(default_factory=dict)  # Out player -> ESPN return date (starts from it)
    objective: str = OBJECTIVE_CATS  # OBJECTIVE_CATS | OBJECTIVE_P_WIN
    first_change_day: date | None = None  # first ET day a move or lineup change made now takes effect (None: days[0])
    tipped: Mapping[date, set[str]] = field(default_factory=dict)  # ET day -> NBA teams whose game has tipped (now)
    _lineups: dict = field(default_factory=dict)

    @property
    def start(self) -> date:
        return max(self.first_change_day or self.days[0], self.days[0])

    def game_days(self, pid: str, start: date | None = None) -> list[date]:
        team = self.pool[pid].nba_team
        return [d for d in self.days if team in self.playing.get(d, set()) and (start is None or d >= start)]

    def add_game_days(self, pid: str, start: date | None = None) -> list[date]:
        """Game days an added player can still play: games that already tipped don't count for him."""
        team = self.pool[pid].nba_team
        return [d for d in self.game_days(pid, start) if team not in self.tipped.get(d, set())]


def score(odds: MatchupOdds, weights: Mapping[str, float]) -> float:
    return float(sum(weights.get(o.category.key, 1.0) * (o.p_win + 0.5 * o.p_tie) for o in odds.categories))


def expected_cats(odds: MatchupOdds, weights: Mapping[str, float]) -> float:
    """Expected categories won over the non-punted categories (weight > 0), each counting 1."""
    return float(sum(o.p_win + 0.5 * o.p_tie for o in odds.categories if weights.get(o.category.key, 1.0) > 0))


def objective_score(odds: MatchupOdds, ctx: StreamContext) -> float:
    return p_win_week(odds) if ctx.objective == OBJECTIVE_P_WIN else score(odds, ctx.weights)


def _units(ctx: StreamContext) -> float:
    """Category-unit thresholds (stream.min_gain, stream.ros_penalty) in the objective's units."""
    return float(C("stream.p_win_per_cat")) if ctx.objective == OBJECTIVE_P_WIN else 1.0


def _in_slot(p: RosterPlayer, held: bool) -> RosterPlayer:
    """The player as placed by the plan: on IL when held, otherwise active (an activated IL player sits on BN)."""
    if held and p.selected not in IL_SLOTS:
        return replace(p, selected="IL")
    if not held and p.selected in IL_SLOTS:
        return replace(p, selected="BN")
    return p


def roster_states(ctx: StreamContext, moves: Sequence[Move]) -> dict[date, DayRoster]:
    """My roster each day after applying moves in day order. From `ctx.start`, IL players able to play are activated
    first, best value first, while an active spot is open (or an Out player can take their IL slot). Raises
    InvalidPlan."""
    ids = [p.player_id for p in ctx.base]
    held = {p.player_id for p in ctx.base if p.selected in IL_SLOTS}
    start = ctx.start
    ordered = sorted(moves, key=lambda m: m.day)
    out: dict[date, DayRoster] = {}
    i = 0
    for d in ctx.days:
        waiting: tuple[str, ...] = ()
        if d >= start and held:
            players = as_of([_in_slot(ctx.pool[x], x in held) for x in ids], d, ctx.return_dates)
            players, _, waiting = activate_ready(players, ctx.capacity, ctx.value)
            held = {p.player_id for p in players if p.selected in IL_SLOTS}
        while i < len(ordered) and ordered[i].day <= d:
            m = ordered[i]
            if m.day < start:
                raise InvalidPlan(f"moves made now take effect from {start}")
            if m.add in ids:
                raise InvalidPlan(f"{m.add} already rostered on {m.day}")
            if m.day < ctx.add_not_before.get(m.add, m.day):
                raise InvalidPlan(f"{m.add} is on waivers until {ctx.add_not_before[m.add]}")
            if m.drop is not None:
                if m.drop not in ids:
                    raise InvalidPlan(f"{m.drop} not rostered on {m.day}")
                if m.drop in held:
                    raise InvalidPlan("cannot drop an IL player to open an active spot")
                ids.remove(m.drop)
            if len([x for x in ids if x not in held]) >= ctx.capacity:
                raise InvalidPlan(f"roster full on {m.day}: a drop is required")
            ids.append(m.add)
            i += 1
        out[d] = DayRoster(tuple(ids), frozenset(held), waiting)
    if i < len(ordered):
        raise InvalidPlan("move outside the planning window")
    return out


def roster_by_day(ctx: StreamContext, moves: Sequence[Move]) -> dict[date, tuple[str, ...]]:
    """Player ids on my roster each day after applying moves in day order. Raises InvalidPlan."""
    return {d: r.ids for d, r in roster_states(ctx, moves).items()}


def _started(ctx: StreamContext, d: date, roster: DayRoster) -> tuple[str, ...]:
    key = (d, frozenset(roster.ids), roster.held)
    cached = ctx._lineups.get(key)
    if cached is None:
        base = {p.player_id for p in ctx.base}
        players = as_of([_in_slot(ctx.pool[pid], pid in roster.held) for pid in roster.ids], d, ctx.return_dates)
        teams = ctx.playing.get(d, set())
        tipped = ctx.tipped.get(d, set())
        playing = {
            p.player_id
            for p in players
            if p.nba_team in teams and (p.player_id in base or p.nba_team not in tipped)  # adds: untipped games only
        }
        fixed = [p.player_id for p in players] if d < ctx.start else ()  # lineup already set for this day
        res = optimize_day(players, ctx.starting_slots, playing, ctx.value, locked=fixed)
        starting = set(ctx.starting_slots)
        cached = tuple(pid for pid, slot in res.assignments.items() if slot in starting and pid in playing)
        ctx._lineups[key] = cached
    return cached


def evaluate(ctx: StreamContext, moves: Sequence[Move]) -> tuple[MatchupOdds, int]:
    states = roster_states(ctx, moves)
    games = [
        (pid, d)
        for d in ctx.days
        for pid in _started(ctx, d, states[d])
        if ctx.pool[pid].nba_team not in ctx.skip.get(d, set())
    ]
    totals = ctx.bank.total(games, ctx.banked)
    return compare(totals, ctx.opp_totals, ctx.categories), len(games)


def waiting_returns(ctx: StreamContext, moves: Sequence[Move]) -> dict[str, date]:
    """IL player -> first ET day he could play but has no active spot under `moves` (a drop is needed)."""
    out: dict[str, date] = {}
    for d, r in roster_states(ctx, moves).items():
        for pid in r.waiting:
            out.setdefault(pid, d)
    return out


def _effective_days(ctx: StreamContext, add: str, drop: str | None, earliest: date) -> list[date]:
    add_days = ctx.add_game_days(add, earliest)
    if not add_days:
        return []
    candidates = {earliest}
    if drop is not None:
        for g in ctx.game_days(drop, earliest):
            nxt = g + timedelta(days=1)
            if nxt <= add_days[-1] and nxt in ctx.days:
                candidates.add(nxt)
    return sorted(d for d in candidates if d <= add_days[-1])


def plan_streams(
    ctx: StreamContext,
    free_agents: Sequence[str],
    droppable: Sequence[str],
    adds_available: int,
    min_gain: float | None = None,
    ros_penalty: float | None = None,
) -> StreamPlan:
    """Greedy plan. `min_gain` / `ros_penalty` are in the objective's units; by default the category-unit constants
    (stream.min_gain, stream.ros_penalty), converted to P(win) units by stream.p_win_per_cat for OBJECTIVE_P_WIN."""
    min_gain = float(C("stream.min_gain")) * _units(ctx) if min_gain is None else min_gain
    ros_penalty = float(C("stream.ros_penalty")) * _units(ctx) if ros_penalty is None else ros_penalty
    base_odds, base_starts = evaluate(ctx, [])
    current_score = objective_score(base_odds, ctx)
    plan = StreamPlan(
        ctx.days,
        base_odds,
        base_odds,
        current_score,
        current_score,
        base_starts,
        base_starts,
        adds_available=adds_available,
        candidates_considered=len(free_agents),
        objective=ctx.objective,
    )
    moves: list[Move] = []
    added: dict[str, date] = {}
    drop_pool = list(droppable)
    current_odds = base_odds
    for _ in range(max(0, adds_available)):
        best: tuple[float, PlannedMove, MatchupOdds, int] | None = None
        rosters = roster_by_day(ctx, moves)
        for add in free_agents:
            if add in added:
                continue
            for drop in [*drop_pool, None]:
                if drop is not None and drop in added:
                    earliest = added[drop] + timedelta(days=1)
                else:
                    earliest = ctx.start
                earliest = max(earliest, ctx.start, ctx.add_not_before.get(add, earliest))
                if earliest > ctx.days[-1]:
                    continue
                for d in _effective_days(ctx, add, drop, earliest):
                    if drop is not None and drop not in rosters[d]:
                        continue
                    candidate = Move(d, add, drop)
                    try:
                        odds, starts = evaluate(ctx, [*moves, candidate])
                    except InvalidPlan:
                        continue
                    gain = objective_score(odds, ctx) - current_score
                    cost = max(0.0, ctx.value.get(drop, 0.0) - ctx.value.get(add, 0.0)) if drop else 0.0
                    objective = gain - ros_penalty * cost
                    if best is None or objective > best[0]:
                        planned = PlannedMove(
                            candidate,
                            gain,
                            cost,
                            len(ctx.add_game_days(add, d)),
                            len(ctx.game_days(drop, d)) if drop else 0,
                            expected_cats(odds, ctx.weights) - expected_cats(current_odds, ctx.weights),
                            p_win_week(odds) - p_win_week(current_odds),
                        )
                        best = (objective, planned, odds, starts)
        if best is None or best[1].gain < min_gain:
            break
        _, planned, odds, starts = best
        moves.append(planned.move)
        added[planned.move.add] = planned.move.day
        drop_pool.append(planned.move.add)
        if planned.move.drop in drop_pool and planned.move.drop not in added:
            drop_pool.remove(planned.move.drop)
        current_score += planned.gain
        current_odds = odds
        plan.moves.append(planned)
        plan.final, plan.final_score, plan.starts_after = odds, current_score, starts
    plan.waiting_returns = waiting_returns(ctx, moves)
    return plan


def with_selected_bn(player: RosterPlayer) -> RosterPlayer:
    return replace(player, selected="BN")
