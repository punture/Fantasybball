"""The league snapshot read through Flaim, with the same LeagueState semantics as the Yahoo XML path.

Flaim's reads are converted to the Yahoo read types and assembled by `fbm.league.yahoo_league.assemble`, so teams,
rosters, slots, categories, standings, waivers, ownership and matchups mean exactly what they mean on the other
routes. Three things Yahoo reports and Flaim may not are filled in here, each flagged in the snapshot warnings:

- week dates: Flaim's own fields when present; else the window FBM recorded for this week number earlier; else the
  Monday-Sunday calendar week (stretched to two weeks when Yahoo's games-left count says the matchup is a merged one,
  as for the NBA Cup or the All-Star break);
- adds used this week: counted from the league's transactions since the week began;
- banked makes/attempts for my matchup: Flaim normally sends Yahoo's own FGM/A and FTM/A rows; only when they are
  missing or empty ("/") mid-week are they rebuilt from each day's lineup and ESPN box scores (fbm.flaim.banked).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fbm.analytics.week import date_range, fantasy_week
from fbm.config import RAW_DIR, LeagueConfig
from fbm.flaim import banked as banked_mod
from fbm.flaim import reads
from fbm.flaim.errors import FlaimError
from fbm.flaim.reads import FlaimLeague
from fbm.league.model import IL_SLOTS, LeagueState
from fbm.timeutil import ET
from fbm.yahoo.ids import IdMapper, Match
from fbm.yahoo.league_reads import Matchup, Transaction

WARNINGS_PATH = RAW_DIR / "flaim_snapshot_warnings.json"
WEEKS_PATH = reads.WEEKS_PATH
TWO_WEEK_RATIO, TWO_WEEK_SLACK = 1.6, 4  # Yahoo's games left > 1.6x the calendar week's + 4 -> a two-week matchup
HISTORY_DAYS = 365  # first transaction sync reads this far back (Flaim allows 366 days per call)

Mapper = Callable[[str, str, str], str | None]  # (yahoo id, name, NBA team) -> ESPN id


@dataclass
class Snapshot:
    league: LeagueState
    summary: dict[str, Any]
    matches: list[Match]
    warnings: list[str] = field(default_factory=list)
    banked_source: dict[str, str] = field(default_factory=dict)  # team_key -> where its banked stats came from
    week_source: str = "flaim"  # flaim | remembered | calendar | calendar (two-week)


# ------------------------------------------------------------------ pure helpers


def calendar_week(day: date, season_start: date | None = None) -> tuple[date, date]:
    """The Monday-Sunday week holding `day`, starting no earlier than the season (Yahoo's week 1 starts on opening
    night)."""
    start, end = fantasy_week(day)
    if season_start and start < season_start <= end:
        start = season_start
    return start, end


def scheduled_games(
    nba_teams: Iterable[str], schedule: Mapping[date, set[str]], days: Iterable[date], per_day_cap: int
) -> int:
    """Player-games the roster's NBA teams play on `days`, at most `per_day_cap` a day (the starting slots)."""
    teams = list(nba_teams)
    return sum(min(sum(1 for t in teams if t in schedule.get(d, set())), per_day_cap) for d in days)


def looks_two_week(games_left: int | None, calendar_left: int) -> bool:
    """Yahoo's games-left count far beyond what the calendar week still holds: the matchup spans two weeks."""
    if games_left is None or calendar_left < 0:
        return False
    return games_left > TWO_WEEK_RATIO * calendar_left + TWO_WEEK_SLACK


def adds_used(txs: Iterable[Transaction], since: datetime, team_keys: Iterable[str]) -> dict[str, int]:
    """Players each team added since `since` (Yahoo's weekly add limit counts players added; trades don't count)."""
    out = dict.fromkeys(team_keys, 0)
    for tx in txs:
        if tx.type not in ("add", "add/drop") or tx.status not in ("successful", ""):
            continue
        if tx.timestamp is None or tx.timestamp < since:
            continue
        for p in tx.players:
            if p.action == "add" and p.destination_team_key in out:
                out[p.destination_team_key] += 1
    return out


def week_start_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(UTC)


# ------------------------------------------------------------------ week window


def _season_start(summary: Mapping[str, Any]) -> date | None:
    return reads.as_date(summary.get("start_date"))


def fill_week(
    league: LeagueState,
    summary: Mapping[str, Any],
    day: date,
    remembered: Mapping[int, tuple[date, date]],
    games_left: int | None,
    schedule: Mapping[date, set[str]] | None,
    warnings: list[str],
    finished: bool = False,
) -> str:
    """Set league.week_start/week_end when Flaim didn't; returns where they came from.

    Yahoo keeps reporting a week that just ended as current until it rolls over (Monday ET). That week is `finished`
    (my matchup final, or no games left): it keeps its own dates, so `fbm.gm.services._roll_week` moves every tool
    to the new week exactly as on the Yahoo routes. A week still running past the window FBM recorded for it is a
    merged (two-week) matchup and is stretched to the end of the current calendar week."""
    if league.week_start and league.week_end:
        return "flaim"
    finished = finished or games_left == 0
    window = remembered.get(league.week)
    if window and (day <= window[1] or finished):
        league.week_start, league.week_end = window
        return "remembered"
    if window:
        league.week_start, league.week_end = window[0], calendar_week(day)[1]
        warnings.append(
            f"week {league.week}: Yahoo still plays week {league.week} after {window[1]} ({games_left} games left): "
            f"a two-week matchup, now {league.week_start}..{league.week_end}"
        )
        return "remembered (two-week)"
    start, end = calendar_week(day - timedelta(days=1) if finished else day, _season_start(summary))
    source = "calendar"
    known = schedule is not None and any(schedule.get(d) for d in date_range(day, end + timedelta(days=7)))
    if known and not finished:
        nba = [p.nba_team for p in league.my_team.players if p.selected not in IL_SLOTS]
        cap = len(league.starting_slots()) or 10
        left = scheduled_games(nba, schedule or {}, date_range(day, end), cap)
        if looks_two_week(games_left, left):
            end += timedelta(days=7)
            source = "calendar (two-week)"
            warnings.append(
                f"week {league.week}: Yahoo reports {games_left} games left but the calendar week holds {left}; "
                f"treating it as a two-week matchup ending {end}"
            )
    elif games_left and not finished:
        warnings.append("no NBA schedule in the local database to check for a two-week matchup: run `fbm data sync`")
    league.week_start, league.week_end = start, end
    warnings.append(f"week {league.week}: dates not sent by Flaim; using {start}..{end} ({source})")
    return source


def remember_week(path: Path, league_key: str, week: int, start: date, end: date) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault(league_key, {})[str(week)] = [start.isoformat(), end.isoformat()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


# ------------------------------------------------------------------ banked stats (my matchup)


def daily_slots(
    reader: FlaimLeague,
    conn: sqlite3.Connection,
    league: LeagueState,
    team: str,
    day: date,
    mapper: Mapper,
    warnings: list[str],
) -> dict[str, dict[str, str]]:
    """{ET date: {player id: slot}} for week_start..day: past days from the stored daily lineups (fetched from Flaim
    once, then kept: a past lineup never changes), today from the snapshot."""
    from fbm.yahoo import lineups

    if league.week_start is None:
        return {}
    past_end = day - timedelta(days=1)
    stored = lineups.daily_lineups(conn, team, league.week_start, past_end) if past_end >= league.week_start else {}
    for d in date_range(league.week_start, past_end):
        if d.isoformat() in stored:
            continue
        try:
            roster = reader.roster(team, d, current=False)
        except FlaimError as err:
            warnings.append(f"lineup {team} {d}: {err.description}")
            continue
        rows = [
            (p.player_key, mapper(p.player_key.rsplit(".p.", 1)[-1], p.name, p.nba_team), p.selected_position)
            for p in roster.players
        ]
        lineups.store_lineup(conn, team, d, rows)
        stored[d.isoformat()] = {eid or f"yahoo:{pk.rsplit('.p.', 1)[-1]}": pos for pk, eid, pos in rows}
    stored[day.isoformat()] = {p.player_id: p.selected for p in league.team(team).players}
    return stored


def bank_matchup(
    league: LeagueState,
    matchups: list[Matchup],
    reader: FlaimLeague,
    conn: sqlite3.Connection,
    day: date,
    mapper: Mapper,
    warnings: list[str],
) -> dict[str, str]:
    """My matchup's banked stats (both teams): Yahoo's own makes/attempts when Flaim sends them (the usual case: the
    display-only FGM/A and FTM/A rows), else completed by the box-score rebuild. Returns the source per team."""
    from fbm.analytics.boxscores import week_lines

    reported = {t.team_key: t.stats for m in matchups if m.status != "preevent" for t in m.teams}
    sources = {}
    for team in (league.my_team_key, league.opponent_key):
        if not team or team not in reported or league.week_start is None:
            continue
        if banked_mod.reports_made_attempted(reported[team]):  # no daily lineups to read, no rebuild
            sources[team] = "yahoo"
            continue
        slots = daily_slots(reader, conn, league, team, day, mapper, warnings)
        ids = {pid for day_slots in slots.values() for pid in day_slots if not pid.startswith("yahoo:")}
        rebuilt = banked_mod.reconstruct(slots, week_lines(conn, ids, league.week_start, day)) if ids else None
        name = next((t.name for t in league.teams if t.team_key == team), team)
        result = banked_mod.merge(reported[team], rebuilt, name)
        league.banked[team] = result.stats
        warnings.extend(result.warnings)
        sources[team] = result.source
    return sources


# ------------------------------------------------------------------ the snapshot


def _mapper(league: LeagueState, ids: IdMapper) -> Mapper:
    rostered = {p.yahoo_key.rsplit(".p.", 1)[-1]: p.player_id for t in league.teams for p in t.players}

    def resolve(yahoo_id: str, name: str, team: str) -> str | None:
        pid = rostered.get(yahoo_id)
        if pid and not pid.startswith("yahoo:"):
            return pid
        return ids.match(yahoo_id, name, team).espn_id

    return resolve


def build_snapshot(
    reader: FlaimLeague,
    cfg: LeagueConfig,
    mapper: IdMapper,
    day: date,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    remembered: Mapping[int, tuple[date, date]] | None = None,
) -> Snapshot:
    """Flaim reads (read-only) -> the league snapshot. `conn` (FBM's DB) enables the week heuristics and the banked
    rebuild; without it those are skipped and noted."""
    from fbm.analytics.week import teams_playing
    from fbm.league.yahoo_league import assemble
    from fbm.yahoo.settings import summarize

    now = now or datetime.now(UTC)
    key = cfg.league_key
    teams = reader.teams(key)
    rosters = {t.team_key: reader.roster(t.team_key, day) for t in teams}
    matchups = reader.scoreboard(key)
    standings = reader.standings(key)
    summary = summarize(reader.settings_raw(key))
    pool = reader.available(key, 100)
    waivers = [p for p in pool if p.ownership_type == "waivers"]
    league, matches = assemble(cfg, summary, teams, rosters, matchups, standings, waivers, mapper, now, pool)
    warnings = list(reader.warnings)
    reader.warnings.clear()
    if not any(t.team_key == league.my_team_key for t in league.teams):
        raise FlaimError(f"your team ({league.my_team_key or 'unknown'}) isn't in {key} on Flaim")

    schedule = None
    if conn is not None:
        _, end = calendar_week(day)
        schedule = teams_playing(conn, day, end + timedelta(days=7))
    games_left = reads.games_remaining(reader.matchups_payload(key), key, league.my_team_key)
    finished = any(m.status == "postevent" and m.team(league.my_team_key) for m in matchups)
    week_source = fill_week(league, summary, day, remembered or {}, games_left, schedule, warnings, finished)

    if not league.adds_used and league.week_start:
        try:
            txs = reader.transactions(key, 100)
            league.adds_used = adds_used(txs, week_start_utc(league.week_start), (t.team_key for t in league.teams))
        except FlaimError as err:
            warnings.append(f"adds used this week: {err.description}")

    sources: dict[str, str] = {}
    if conn is not None:
        sources = bank_matchup(league, matchups, reader, conn, day, _mapper(league, mapper), warnings)
    elif not all(
        banked_mod.reports_made_attempted(t.stats) for m in matchups if m.status != "preevent" for t in m.teams
    ):
        warnings.append("banked makes/attempts: no local database, Yahoo's reported totals only")
    warnings.extend(reader.warnings)
    reader.warnings.clear()
    return Snapshot(league, summary, matches, warnings, sources, week_source)


def read_snapshot(
    cfg: LeagueConfig, mapper: IdMapper, day: date, conn: sqlite3.Connection
) -> tuple[LeagueState, dict[str, Any], list[Match]]:
    """`fbm.league.yahoo_league.refresh` on the Flaim route: read, then remember the week window and the warnings."""
    with FlaimLeague.from_config() as reader:
        snap = build_snapshot(reader, cfg, mapper, day, conn, remembered=reader.week_windows)
    if snap.league.week_start and snap.league.week_end:
        remember_week(WEEKS_PATH, cfg.league_key, snap.league.week, snap.league.week_start, snap.league.week_end)
    save_warnings(snap)
    return snap.league, snap.summary, snap.matches


def save_warnings(snap: Snapshot) -> None:
    WARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "fetched_at": snap.league.fetched_at,
        "week": snap.league.week,
        "week_window": [str(snap.league.week_start), str(snap.league.week_end)],
        "week_source": snap.week_source,
        "banked_source": snap.banked_source,
        "warnings": snap.warnings,
    }
    WARNINGS_PATH.write_text(json.dumps(body, indent=1), encoding="utf-8")


def last_warnings() -> dict[str, Any]:
    try:
        return json.loads(WARNINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ transaction history (intel, verification)


def sync_transactions(conn: sqlite3.Connection, reader: FlaimLeague, today: date | None = None) -> dict[str, Any]:
    """`fbm.league.tx_history.sync` on the Flaim route. Flaim pages by date, not offset: read from the day before
    the newest stored transaction (or a year back on the first sync) through today, in one call."""
    from fbm.league import tx_history

    today = today or datetime.now(UTC).date()
    latest = conn.execute("SELECT MAX(ts_utc) FROM league_transactions").fetchone()[0]
    start = datetime.fromisoformat(latest).date() - timedelta(days=1) if latest else None
    start = max(start or date.min, today - timedelta(days=HISTORY_DAYS))  # Flaim reads at most 366 days per call
    txs = reader.transactions_between(start, today)
    new = tx_history.store(conn, txs)
    return {
        "pages": 1,
        "fetched": len(txs),
        "new": new,
        "total_stored": len(tx_history.known_keys(conn)),
        "complete": True,
        "window": [start.isoformat(), today.isoformat()],
    }
