"""Turn rosters + the NBA schedule into simulated weeks (per-day optimal lineups -> started games), plus the league
rules and objectives week decisions are judged by (lineup deadline, P(win the matchup))."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import numpy as np

from fbm.analytics.categories import Category
from fbm.analytics.lineup import LineupResult, activate_ready, as_of, optimize_day
from fbm.analytics.simulation import MatchupOdds, TeamWeek, category_values
from fbm.constants import C
from fbm.league.model import IL_SLOTS, FantasyTeam


def fantasy_week(day: date) -> tuple[date, date]:
    """Monday-Sunday week containing `day` (Yahoo NBA weeks run Mon-Sun ET)."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def teams_playing(conn: sqlite3.Connection, start: date, end: date) -> dict[date, set[str]]:
    out: dict[date, set[str]] = {d: set() for d in date_range(start, end)}
    for row in conn.execute(
        "SELECT et_date, home, away FROM games WHERE season_type = 2 AND et_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ):
        d = date.fromisoformat(row["et_date"])
        out.setdefault(d, set()).update((row["home"], row["away"]))
    return out


# ------------------------------------------------------------------ league rules and objectives


def lineup_deadline(summary: Mapping[str, Any] | None, override: str = "auto") -> str:
    """When lineup changes and adds take effect: "today" (Yahoo "Daily - Today": the same ET day, each player locks
    at his tip) or "tomorrow" ("Daily - Tomorrow": the next ET day). `override` "today"/"tomorrow" wins (constant
    `lineup.deadline`); "auto" reads Yahoo's `weekly_deadline` from the settings summary (league or settings level).
    Only a tomorrow-style code means tomorrow: empty, "intraday", "today" and unknown codes keep "today"."""
    chosen = (override or "auto").strip().lower()
    if chosen in ("today", "tomorrow"):
        return chosen
    summary = summary or {}
    raw = summary.get("weekly_deadline") or (summary.get("settings") or {}).get("weekly_deadline") or ""
    code = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return "tomorrow" if "tomorrow" in code or code in ("next_day", "nextday") else "today"


def first_change_day(today: date, deadline: str) -> date:
    """First ET day a lineup change, add or drop made now takes effect."""
    return today + timedelta(days=1) if deadline == "tomorrow" else today


def p_win_week(odds: MatchupOdds) -> float:
    """P(win the matchup) in a one-win-per-week (Most Categories) league: more categories won than lost; a tie on
    the category count is a tied week and counts half."""
    tie = max(0.0, 1.0 - odds.p_majority - odds.p_majority_loss)
    return float(odds.p_majority + 0.5 * tie)


def _week_result(margin: np.ndarray) -> np.ndarray:
    return np.where(margin > 0, 1.0, np.where(margin == 0, 0.5, 0.0))


def pivotal_weights(
    totals_a: np.ndarray, totals_b: np.ndarray, categories: Sequence[Category], punts: Collection[str] = ()
) -> dict[str, float]:
    """How much each category decides the week in a Most Categories league, from simulated weekly totals (same rows
    = same simulated week): the average change in the week's result (win 1, tie 0.5) when that category flips from a
    loss to a win, the others as simulated. Scaled so the most pivotal category weighs 1; punts get 0. These are
    the first-order weights of P(win the matchup) in each category's win probability."""
    outcomes = []
    for cat in categories:
        a, b = category_values(totals_a, cat), category_values(totals_b, cat)
        diff = a - b if cat.higher_is_better else b - a
        outcomes.append(np.where(np.isclose(a, b, rtol=0, atol=1e-9), 0.0, np.sign(diff)))
    if not outcomes:
        return {}
    grid = np.array(outcomes)  # categories x sims: +1 won, 0 tied, -1 lost
    margin = grid.sum(axis=0)
    raw = {}
    for i, cat in enumerate(categories):
        others = margin - grid[i]
        raw[cat.key] = 0.0 if cat.key in punts else float(np.mean(_week_result(others + 1) - _week_result(others - 1)))
    top = max(raw.values(), default=0.0)
    return {k: (v / top if top > 0 else 0.0) for k, v in raw.items()}


# ------------------------------------------------------------------ week plans


@dataclass(frozen=True)
class DayPlan:
    day: date
    lineup: LineupResult
    started: tuple[str, ...]  # player_ids starting with a game
    activated: tuple[str, ...] = ()  # IL players moved to the bench this day (healthy or back from injury)
    waiting: tuple[str, ...] = ()  # IL players able to play with no active spot for them (a drop is needed)


def plan_team_week(
    team: FantasyTeam,
    days: list[date],
    playing_by_date: Mapping[date, set[str]],
    starting_slots: list[str],
    value: Mapping[str, float],
    return_dates: Mapping[str, date] | None = None,
    capacity: int | None = None,
    locked_days: Collection[date] = (),
    not_playing: Mapping[date, Collection[str]] | None = None,
) -> tuple[TeamWeek, list[DayPlan]]:
    """Daily optimal lineups for `days`. `return_dates` (player_id -> ESPN return date): an Out player can start
    from his return date (the simulation weights those games by his return play probability).

    capacity (active roster spots): an IL player able to play (healthy, or from his return date) is activated once a
    spot is open (`activate_ready`) and stays active; one left without a spot is listed in `DayPlan.waiting` (a drop
    is needed). None: IL players stay on IL all week.
    locked_days: ET days whose lineup can no longer change (Daily - Tomorrow leagues: today): the lineup as set stands.
    not_playing: ET day -> player_ids whose game that day doesn't count (e.g. a player added after his game tipped).
    """
    starting = set(starting_slots)
    locked = set(locked_days)
    current = list(team.players)
    games: list[str] = []
    game_dates: list[date] = []
    plans: list[DayPlan] = []
    for d in days:
        nba_teams = playing_by_date.get(d, set())
        roster = as_of(current, d, return_dates)
        activated: tuple[str, ...] = ()
        waiting: tuple[str, ...] = ()
        if capacity is not None and d not in locked:
            roster, activated, waiting = activate_ready(roster, capacity, value)
            slot = {p.player_id: p.selected for p in roster}  # activations and swaps persist for later days
            current = [
                p if p.selected == slot[p.player_id] else replace(p, selected=slot[p.player_id]) for p in current
            ]
        skip = set((not_playing or {}).get(d, ()))
        playing = {p.player_id for p in roster if p.nba_team in nba_teams and p.player_id not in skip}
        fixed = [p.player_id for p in roster] if d in locked else ()
        res = optimize_day(roster, starting_slots, playing, value, locked=fixed)
        started = tuple(pid for pid, slot in res.assignments.items() if slot in starting and pid in playing)
        games.extend(started)
        game_dates.extend([d] * len(started))
        plans.append(DayPlan(d, res, started, activated, waiting))
    return TeamWeek(team.team_key, games, game_dates=game_dates), plans


def waiting_returns(plans: Sequence[DayPlan]) -> dict[str, date]:
    """IL player -> first ET day he could play but had no active roster spot (a drop or bench decision is needed)."""
    out: dict[str, date] = {}
    for plan in plans:
        for pid in plan.waiting:
            out.setdefault(pid, plan.day)
    return out


def generic_week(team: FantasyTeam, n_starting: int, games_per_player: float | None = None) -> TeamWeek:
    """Schedule-neutral week for season-level comparisons: every active player plays the average number of games
    (week.avg_games_per_week), scaled by the share of roster spots that can start."""
    per_player = float(C("week.avg_games_per_week")) if games_per_player is None else games_per_player
    active = [p for p in team.players if p.selected not in IL_SLOTS]
    if not active:
        return TeamWeek(team.team_key, [])
    games = 4
    share = min(1.0, n_starting / len(active)) * per_player / games
    return TeamWeek(
        team.team_key,
        [p.player_id for p in active for _ in range(games)],
        play_scale={p.player_id: share for p in active},
    )
