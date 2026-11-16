"""Fantasy playoff schedule: which NBA teams play the most games in the fantasy playoff weeks.

Fantasy weeks run Monday-Sunday (ET) from the week of the first regular-season game, except Yahoo's merged matchup
periods (NBA Cup, All-Star break: `playoffs.merged_weeks`), and Yahoo's real bounds for a known week (the league
snapshot's current week) anchor the numbering. The playoffs are the league's playoff weeks from Yahoo settings when
available, otherwise `playoffs.weeks` weeks from `playoffs.default_start_week` (Yahoo's default).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fbm.constants import C

Period = tuple[date, date]
Anchor = tuple[int, date, date]  # (week number, start, end) of a week whose real Yahoo bounds are known


@dataclass(frozen=True)
class FantasyWeek:
    number: int
    start: date
    end: date


def merged_periods(spec: str | None = None) -> list[Period]:
    """Yahoo matchup periods longer than a week from 'YYYY-MM-DD..YYYY-MM-DD' ranges, comma-separated (default: the
    `playoffs.merged_weeks` constant). Malformed or reversed ranges are ignored."""
    text = str(C("playoffs.merged_weeks")) if spec is None else spec
    out: list[Period] = []
    for part in text.split(","):
        lo, sep, hi = part.strip().partition("..")
        try:
            period = (date.fromisoformat(lo.strip()), date.fromisoformat(hi.strip()))
        except ValueError:
            continue
        if sep and period[0] <= period[1]:
            out.append(period)
    return sorted(out)


def _calendar(start: date, last: date, merged: Sequence[Period], number: int) -> list[FantasyWeek]:
    """Weeks from `start` through the week containing `last`: Monday-Sunday, except that a week touching a merged
    period runs to the period's end."""
    weeks: list[FantasyWeek] = []
    while start <= last:
        end = start + timedelta(days=6)
        extended = True
        while extended:
            extended = False
            for lo, hi in merged:
                if lo <= end < hi:
                    end, extended = hi, True
        weeks.append(FantasyWeek(number, start, end))
        start, number = end + timedelta(days=1), number + 1
    return weeks


def build_weeks(
    first_game: date, last_game: date, merged: Sequence[Period] = (), anchor: Anchor | None = None
) -> list[FantasyWeek]:
    """Fantasy weeks covering the regular season (pure). Week 1 starts the Monday of opening week. `anchor` = a week
    whose real bounds are known: it keeps them and its number, later weeks follow from its end, earlier weeks are
    renumbered back from it (weeks numbered below 1 are dropped)."""
    weeks = _calendar(first_game - timedelta(days=first_game.weekday()), last_game, merged, 1)
    if anchor is None:
        return weeks
    number, start, end = anchor
    if end < start:
        return weeks
    before = [w for w in weeks if w.end < start]
    first = number - len(before)
    renumbered = [FantasyWeek(first + i, w.start, w.end) for i, w in enumerate(before)]
    after = _calendar(end + timedelta(days=1), last_game, merged, number + 1)
    return [w for w in renumbered if w.number >= 1] + [FantasyWeek(number, start, end), *after]


def fantasy_weeks(
    conn: sqlite3.Connection,
    season: int,
    merged: Sequence[Period] | None = None,
    anchor: Anchor | None = None,
) -> list[FantasyWeek]:
    """The season's fantasy weeks from the stored NBA schedule (`build_weeks`); `merged` defaults to the
    `playoffs.merged_weeks` constant."""
    row = conn.execute(
        "SELECT MIN(et_date), MAX(et_date) FROM games WHERE season = ? AND season_type = 2", (season,)
    ).fetchone()
    if not row or not row[0]:
        return []
    first, last = date.fromisoformat(row[0]), date.fromisoformat(row[1])
    return build_weeks(first, last, merged_periods() if merged is None else merged, anchor)


def playoff_weeks(weeks: list[FantasyWeek], settings: Mapping[str, Any] | None = None) -> list[FantasyWeek]:
    s = (settings or {}).get("settings", {}) if settings else {}
    start_week = s.get("playoff_start_week")
    end_week = (settings or {}).get("end_week")
    if start_week and str(s.get("uses_playoff", "1")) == "1":
        lo, hi = int(start_week), int(end_week or weeks[-1].number)
        chosen = [w for w in weeks if lo <= w.number <= hi]
        if chosen:
            return chosen
    n = int(C("playoffs.weeks"))
    lo = int(C("playoffs.default_start_week"))
    default = [w for w in weeks if lo <= w.number < lo + n]
    if len(default) == n:
        return default
    return weeks[-n:] if weeks else []


def team_games_by_week(conn: sqlite3.Connection, weeks: Iterable[FantasyWeek]) -> dict[str, list[int]]:
    weeks = list(weeks)
    out: dict[str, list[int]] = {}
    for i, w in enumerate(weeks):
        for row in conn.execute(
            "SELECT home, away FROM games WHERE season_type = 2 AND et_date BETWEEN ? AND ?",
            (w.start.isoformat(), w.end.isoformat()),
        ):
            for team in (row["home"], row["away"]):
                out.setdefault(team, [0] * len(weeks))[i] += 1
    return out


@dataclass(frozen=True)
class PlayoffSchedule:
    weeks: list[FantasyWeek]
    team_games: dict[str, list[int]]

    @property
    def totals(self) -> dict[str, int]:
        return {t: sum(g) for t, g in self.team_games.items()}

    @property
    def average_total(self) -> float:
        totals = list(self.totals.values())
        return sum(totals) / len(totals) if totals else 0.0

    def games(self, team: str) -> int:
        return self.totals.get(team, 0)


def playoff_schedule(
    conn: sqlite3.Connection, season: int, settings: Mapping[str, Any] | None = None, anchor: Anchor | None = None
) -> PlayoffSchedule:
    """`anchor`: a week whose real Yahoo bounds are known (the league snapshot's week), see `build_weeks`."""
    weeks = playoff_weeks(fantasy_weeks(conn, season, anchor=anchor), settings)
    return PlayoffSchedule(weeks, team_games_by_week(conn, weeks))


def playoff_value(per_game_value: float, games: int, average_games: float) -> float:
    """Playoff-weeks value relative to an average schedule: value x games / average games."""
    return per_game_value * games / average_games if average_games else 0.0


def season_progress(today: date, season_start: date, playoff_start: date) -> float:
    """0 at opening night, 1 once the playoffs begin (used to weight playoff schedules in trades)."""
    span = (playoff_start - season_start).days
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (today - season_start).days / span))
