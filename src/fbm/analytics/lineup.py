"""Daily lineup optimizer (max expected games started, then value), solved as a max-weight bipartite assignment."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

import numpy as np
from scipy.optimize import linear_sum_assignment

from fbm.analytics.simulation import play_probability
from fbm.constants import C
from fbm.league.model import IL_SLOTS, RosterPlayer, can_fill

GAME_WEIGHT = 1000.0  # a started game (x the player's play probability) always beats any value difference
STABILITY_BONUS = 0.01  # prefer leaving players where they are (fewer manual changes)
INELIGIBLE = -1e9


@dataclass(frozen=True)
class LineupResult:
    assignments: dict[str, str]  # player_id -> slot (starting slot, BN, or IL slot)
    starters_with_games: int
    benched_with_games: tuple[str, ...]
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)  # player_id -> (old, new)

    def moves(self, players: Iterable[RosterPlayer]) -> list[tuple[str, str, str]]:
        names = {p.player_id: p.name for p in players}
        return [(names.get(pid, pid), old, new) for pid, (old, new) in self.changes.items()]


def as_of(
    players: Iterable[RosterPlayer], day: date, return_dates: Mapping[str, date] | None = None
) -> list[RosterPlayer]:
    """Roster as of ET `day` for week planning: an Out player whose ESPN return date is on or before `day` is
    treated as available (status cleared), so the lineup can start him from his return."""
    if not return_dates:
        return list(players)
    out = []
    for p in players:
        back = return_dates.get(p.player_id)
        out.append(replace(p, status="") if p.is_out and back is not None and back <= day else p)
    return out


def optimize_day(
    players: list[RosterPlayer],
    starting_slots: list[str],
    playing: set[str],
    value: Mapping[str, float],
    locked: Iterable[str] = (),
    sit: Iterable[str] = (),
) -> LineupResult:
    """Assign players to `starting_slots` for one game day.

    playing: player_ids whose NBA team has a game that day.
    locked: player_ids whose game has started (their slot cannot change).
    sit: player_ids the caller explicitly benches (e.g. poor shooters while FG%/FT% is contested): they take no
    starting slot, even an open one. Locked players keep their slot.
    A game counts at the player's play probability (injury status: GTD/DTD/questionable < 1), so with too few slots a
    healthy player with a game starts over a game-time decision, and any game still beats no game.
    Players currently in IL/IL+ stay there (IL moves are handled separately: `il_moves`, `activate_ready`).
    """
    locked_ids = set(locked)
    sit_ids = set(sit) - locked_ids
    il_players = [p for p in players if p.selected in IL_SLOTS]
    locked_players = [p for p in players if p.player_id in locked_ids and p.selected not in IL_SLOTS]
    open_slots = list(starting_slots)
    for p in locked_players:
        if p.selected in open_slots:
            open_slots.remove(p.selected)
    candidates = [
        p
        for p in players
        if p.player_id not in locked_ids and p.player_id not in sit_ids and p.selected not in IL_SLOTS
    ]

    def has_game(p: RosterPlayer) -> bool:
        return p.player_id in playing and not p.is_out

    assignments: dict[str, str] = {p.player_id: p.selected for p in il_players + locked_players}
    if candidates and open_slots:
        weights = np.full((len(candidates), len(open_slots)), INELIGIBLE)
        for i, p in enumerate(candidates):
            game = GAME_WEIGHT * play_probability(p.status) if has_game(p) else 0.0
            base = game + float(value.get(p.player_id, 0.0))
            for j, slot in enumerate(open_slots):
                if can_fill(p.eligible, slot):
                    weights[i, j] = base + (STABILITY_BONUS if p.selected == slot else 0.0)
        rows, cols = linear_sum_assignment(weights, maximize=True)
        for i, j in zip(rows, cols, strict=True):
            if weights[i, j] > INELIGIBLE / 2:
                assignments[candidates[i].player_id] = open_slots[j]
    for p in players:
        assignments.setdefault(p.player_id, "BN")  # unassigned candidates and benched (`sit`) players

    starting = set(starting_slots)
    starters_with_games = sum(1 for p in players if assignments[p.player_id] in starting and has_game(p))
    benched = tuple(p.player_id for p in players if assignments[p.player_id] == "BN" and has_game(p))
    changes = {
        p.player_id: (p.selected, assignments[p.player_id]) for p in players if p.selected != assignments[p.player_id]
    }
    return LineupResult(assignments, starters_with_games, benched, changes)


def expected_out_today(p: RosterPlayer, playing: set[str] | None = None) -> bool:
    """Is `p` not expected to play today? Ruled out (O/INJ/OFS/...), a status whose play probability is at most
    `lineup.il_stash_max_play_prob` (doubtful), or any injury status (GTD/DTD/questionable) with no game today.
    `playing`: player_ids with a game today (None = unknown: a game-time decision is then assumed to play)."""
    if p.is_out:
        return True
    prob = play_probability(p.status)
    if prob >= 1.0:
        return False
    if prob <= float(C("lineup.il_stash_max_play_prob")):
        return True
    return playing is not None and p.player_id not in playing


@dataclass(frozen=True)
class IlMove:
    player_id: str
    name: str
    from_slot: str
    to_slot: str
    reason: str
    blocked: bool = False  # needs an approval-gated drop before it can happen


def il_moves(
    players: list[RosterPlayer],
    il_slot_counts: Mapping[str, int],
    capacity: int | None = None,
    locked: Iterable[str] = (),
    playing: set[str] | None = None,
) -> list[IlMove]:
    """IL compliance moves for one ET day.

    1. Stash: active IL/IL+-eligible players not expected to play today (`expected_out_today`: ruled out, doubtful,
       or a game-time decision with no game) go into open IL slots (frees active spots). A game-time decision with a
       game today stays active: Yahoo's IL+ accepts him, but stashing him would cost the game.
    2. Activate: IL players no longer IL-eligible, then IL players whose status improved enough to play today and who
       have a game (`playing`: player_ids with a game today), move to the bench if an active spot is open. Otherwise
       an ineligible player's move is `blocked` (a drop must be proposed; while he sits in IL, Yahoo blocks every
       roster move until it is resolved); a still-eligible player simply stays on IL (no move: nothing is blocked,
       and the week plan's `waiting` reports the spot he needs).
    Players whose games have started (`locked`) are left alone.
    """
    locked_ids = set(locked)
    moves: list[IlMove] = []
    used = {slot: sum(1 for p in players if p.selected == slot) for slot in il_slot_counts}
    active = sum(1 for p in players if p.selected not in IL_SLOTS)
    injured = [
        p
        for p in players
        if p.selected not in IL_SLOTS
        and p.player_id not in locked_ids
        and any(s in p.eligible for s in il_slot_counts)
        and expected_out_today(p, playing)
    ]
    in_il = [p for p in players if p.selected in IL_SLOTS and p.player_id not in locked_ids]
    must = [p for p in in_il if p.selected not in p.eligible]
    improved = [
        p
        for p in in_il
        if p not in must and playing is not None and p.player_id in playing and not expected_out_today(p, playing)
    ]

    def stash(p: RosterPlayer, slot: str) -> None:
        nonlocal active
        moves.append(IlMove(p.player_id, p.name, p.selected, slot, f"{p.status or 'injured'}; frees a spot"))
        injured.remove(p)
        active -= 1

    def stash_into_open_slots() -> None:
        for p in list(injured):
            for slot in ("IL", "IL+"):
                if slot in p.eligible and used.get(slot, 0) < il_slot_counts.get(slot, 0):
                    stash(p, slot)
                    used[slot] = used.get(slot, 0) + 1
                    break

    # 1. Stash injured players into currently open IL slots.
    stash_into_open_slots()
    # 2. Activate: swap with an injured player for the same slot, else use an open active spot.
    for p in [*must, *improved]:
        why = "healthy: activate from IL" if p in must else f"{p.status or 'healthy'} with a game today: activate"
        swap = next((q for q in injured if p.selected in q.eligible), None)
        if swap is not None:
            stash(swap, p.selected)
            moves.append(IlMove(p.player_id, p.name, p.selected, "BN", why))
            active += 1
        elif capacity is None or active < capacity:
            moves.append(IlMove(p.player_id, p.name, p.selected, "BN", why))
            used[p.selected] -= 1
            active += 1
        elif p in must:
            reason = "healthy but the roster is full: drop someone to activate (proposal needed)"
            moves.append(IlMove(p.player_id, p.name, p.selected, p.selected, reason, True))
        # else: still IL-eligible, so nothing is blocked: he stays on IL (week plans report the spot he needs)
    # 3. Stash remaining injured players into slots freed by activations.
    stash_into_open_slots()
    return moves


def activate_ready(
    players: Sequence[RosterPlayer], capacity: int, value: Mapping[str, float] | None = None
) -> tuple[list[RosterPlayer], tuple[str, ...], tuple[str, ...]]:
    """Week planning: IL/IL+ players able to play (not `expected_out_today`: healthy, a game-time decision, or back
    from their ESPN return date via `as_of`; Out, out-for-season and doubtful players stay) move to the bench, best
    value first, while an active roster spot is open (Yahoo needs one to activate a player) or an active player not
    expected to play who is eligible for the freed IL slot can take it (a swap). `capacity` = active (non-IL) spots.

    Returns (roster, activated ids, waiting ids): waiting players are ready to play but have no spot (a drop is
    needed). The input players are not mutated."""
    value = value or {}
    out = list(players)
    index = {p.player_id: i for i, p in enumerate(out)}
    active = sum(1 for p in out if p.selected not in IL_SLOTS)
    ready = sorted(
        (p for p in out if p.selected in IL_SLOTS and not expected_out_today(p)),
        key=lambda p: -float(value.get(p.player_id, 0.0)),
    )
    activated: list[str] = []
    waiting: list[str] = []
    for r in ready:
        if active < capacity:
            active += 1
        else:
            swap = next(
                (q for q in out if q.selected not in IL_SLOTS and expected_out_today(q) and r.selected in q.eligible),
                None,
            )
            if swap is None:
                waiting.append(r.player_id)
                continue
            out[index[swap.player_id]] = replace(swap, selected=r.selected)
        out[index[r.player_id]] = replace(r, selected="BN")
        activated.append(r.player_id)
    return out, tuple(activated), tuple(waiting)


def _tip_time(raw: object) -> datetime | None:
    """Scheduled tip time (UTC) from the schedule's start_utc; None when unreadable."""
    try:
        start = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return start if start.tzinfo is not None else start.replace(tzinfo=UTC)


def started_teams(games: Iterable[Mapping[str, object]], now: datetime) -> set[str]:
    """NBA teams whose game on this ET day has started (by state or scheduled tip time) — their players are locked,
    and a player added now can't play that game. An unreadable tip time falls back to the game state."""
    out: set[str] = set()
    for g in games:
        start = _tip_time(g["start_utc"])
        if g["state"] in ("in", "post") or (start is not None and start <= now):
            out.update((str(g["home"]), str(g["away"])))
    return out
