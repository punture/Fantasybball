"""Which fantasy week is it? One pure resolver so the week number, its dates and the opponent always agree.

Yahoo keeps reporting a finished week as current (status `midevent`) for a while after its last day (e.g. Monday ET
before it rolls over), while the calendar already says the next week. `effective_week` settles that from today's ET
date plus Yahoo's own week bounds: past the end of Yahoo's week, the effective week is the next Yahoo week (its
bounds and opponent come from the Yahoo schedule via `lookup`; the Monday-Sunday calendar is only the fallback).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta

from fbm.analytics.week import fantasy_week
from fbm.yahoo.league_reads import Matchup

MAX_ROLLS = 4  # a snapshot older than this many weeks is not worth rolling by schedule lookups


@dataclass(frozen=True)
class WeekInfo:
    number: int
    # start/end are None only from a schedule lookup that names the pairing but not the dates (Flaim sends no dates
    # for a week that isn't current yet); `next_week_info` then takes the calendar's dates.
    start: date | None
    end: date | None
    opponent_key: str | None = None
    # yahoo (snapshot) | yahoo schedule | yahoo pairing + calendar (Yahoo's opponent on the calendar's dates) |
    # calendar (Monday-Sunday after the previous week; NBA Cup / All-Star weeks merged)
    source: str = "yahoo"


@dataclass(frozen=True)
class EffectiveWeek:
    week: WeekInfo
    rolled_from: WeekInfo | None  # Yahoo's reported (finished) week when `week` is the next one; else None
    note: str = ""


WeekLookup = Callable[[int], "WeekInfo | None"]  # Yahoo schedule: week number -> its info, None when unavailable

_SOURCE_NOTES = {
    "yahoo schedule": "Yahoo schedule",
    "yahoo pairing + calendar": "opponent from Yahoo's schedule; dates from the Monday-Sunday calendar",
    "calendar": "Monday-Sunday calendar: Yahoo's schedule was unavailable, so the opponent is unknown",
}


def week_info_from_matchups(week: int, matchups: Iterable[Matchup], my_team_key: str) -> WeekInfo | None:
    """Bounds from any matchup of that Yahoo week (they share them); the opponent from mine (None on a bye).

    Undated matchups (Flaim, for a week that isn't current yet) still give the opponent, with start/end None.
    Matchups labeled with another week are ignored, so a feed that answers with the current week never names the
    next week's opponent."""
    matchups = [m for m in matchups if m.week in (None, week)]
    if not matchups:
        return None
    dated = next((m for m in matchups if m.week_start and m.week_end), None)
    start, end = (dated.week_start, dated.week_end) if dated else (None, None)
    mine = next((m for m in matchups if m.team(my_team_key)), None)
    opp = mine.opponent_of(my_team_key) if mine else None
    return WeekInfo(week, start, end, opp.team_key if opp else None, "yahoo schedule")


def calendar_week_after(prev: WeekInfo) -> WeekInfo:
    if prev.end is None:
        raise ValueError(f"week {prev.number} has no end date")
    start = prev.end + timedelta(days=1)
    return WeekInfo(prev.number + 1, start, fantasy_week(start)[1], None, "calendar")


def next_week_info(week: WeekInfo, lookup: WeekLookup | None = None) -> WeekInfo:
    """The week after `week`: Yahoo's schedule entry when it is consistent (right number, starts after `week` ends),
    else the Monday-Sunday calendar week. An undated entry (Flaim) keeps Yahoo's opponent on the calendar's dates."""
    cal = calendar_week_after(week)
    info = lookup(cal.number) if lookup else None
    if info is None or info.number != cal.number:
        return cal
    if info.start is None or info.end is None:
        return replace(cal, opponent_key=info.opponent_key, source="yahoo pairing + calendar")
    if cal.start is not None and info.start >= cal.start and info.end >= info.start:
        return info
    return cal


def effective_week(yahoo: WeekInfo, today: date, lookup: WeekLookup | None = None) -> EffectiveWeek:
    """The fantasy week `today` (ET) belongs to, given Yahoo's reported current week."""
    week = yahoo
    for _ in range(MAX_ROLLS):
        if week.end is None or today <= week.end:
            break
        week = next_week_info(week, lookup)
    if week is yahoo:
        return EffectiveWeek(yahoo, None)
    note = (
        f"Yahoo still reports week {yahoo.number} ({yahoo.start}..{yahoo.end}) as current and has not rolled over; "
        f"FBM treats week {week.number} ({week.start}..{week.end}) as this week "
        f"({_SOURCE_NOTES.get(week.source, _SOURCE_NOTES['yahoo schedule'])})."
    )
    return EffectiveWeek(week, yahoo, note)
