"""Flaim payloads -> FBM's Yahoo read types (the same dataclasses the Yahoo XML parsers return).

Flaim normalizes Yahoo's API into JSON. The shapes here follow Flaim's published tool schemas; fields those schemas
leave open (category rows, roster slots, team details) are read defensively under several plausible names, and
anything missing is recorded in `FlaimLeague.warnings` (shown by `fbm flaim check`) instead of failing the read.

`FlaimLeague` is the object the `fbm.yahoo` fetch functions accept on the Flaim route: they call its methods instead
of Yahoo's v2 paths. Read-only, like everything in FBM.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fbm.config import RAW_DIR
from fbm.flaim.client import FlaimClient
from fbm.flaim.errors import FlaimError, FlaimUnavailable
from fbm.league.model import with_il_eligibility
from fbm.yahoo.league_reads import Matchup, MatchupTeam, StandingRow, Transaction, TxPlayer, YahooPlayer, YahooTeam
from fbm.yahoo.roster import Roster, RosterPlayer

PLATFORM, SPORT = "yahoo", "basketball"
MATCHUP_STATUS = {"upcoming": "preevent", "in_progress": "midevent", "final": "postevent"}  # Flaim -> Yahoo
TX_STATUS = {"complete": "successful", "failed": "failed", "pending": "pending", "canceled": "canceled", "unknown": ""}
OPEN_OFFER = "proposed"  # Yahoo's status of an offer awaiting an answer
ACQUISITION = {"waivers": "waivers", "free_agent": "freeagents"}  # Flaim acquisitionState -> Yahoo ownership_type
# Yahoo NBA stat ids by the labels Flaim (or Yahoo) may use for a category row.
LABEL_STAT_IDS = {
    "FGM/A": "9004003",
    "FGM/FGA": "9004003",
    "FG%": "5",
    "FTM/A": "9007006",
    "FTM/FTA": "9007006",
    "FT%": "8",
    "3PTM": "10",
    "3PM": "10",
    "PTS": "12",
    "REB": "15",
    "AST": "16",
    "ST": "17",
    "STL": "17",
    "BLK": "18",
    "TO": "19",
    "TOV": "19",
}
STAT_LABELS = {sid: label for label, sid in reversed(LABEL_STAT_IDS.items())}  # Yahoo's own label (first listed)
DISPLAY_ONLY = frozenset({"9004003", "9007006"})  # makes/attempts: shown by Yahoo, never scored
LOWER_IS_BETTER = frozenset({"19"})
BENCH_SLOTS = frozenset({"BN", "IL", "IL+", "NA"})
SIDES = ("home", "away")  # Flaim's matchup sides
_SLOT_ALIASES = {"UTIL": "UTIL", "IR": "IL", "IR+": "IL+", "BENCH": "BN"}


# ------------------------------------------------------------------ tolerant field access


def pick(d: Any, *names: str, default: Any = None) -> Any:
    """The first of `names` present in `d` with a usable value."""
    if not isinstance(d, Mapping):
        return default
    for name in names:
        value = d.get(name)
        if value is not None and value != "":
            return value
    return default


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, OverflowError):  # "", "-", "inf", "nan"
        return None


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def as_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def records(value: Any) -> list[dict[str, Any]]:
    """A list of dict rows from a list, or from a dict keyed by id/label (the key kept as `_key`)."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, Mapping):
        return [{"_key": k, **v} if isinstance(v, Mapping) else {"_key": k, "value": v} for k, v in value.items()]
    return []


def data_of(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
        return dict(payload["data"])
    return dict(payload) if isinstance(payload, Mapping) else {}


def game_key_of(league_key: str) -> str:
    return (league_key or "").split(".l.", 1)[0]


def team_key(league_key: str, raw: Any) -> str:
    """Flaim may send a full Yahoo team key ("466.l.1.t.3") or the team id ("3")."""
    text = str(raw or "").strip()
    if not text or "." in text or not league_key:
        return text
    return f"{league_key}.t.{text}"


def _name(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(pick(value, "full", "name", default="") or "")
    return str(value or "")


def _player_key(p: Mapping[str, Any], game_key: str) -> tuple[str, str]:
    """(player_key, numeric Yahoo player id)."""
    key = str(pick(p, "playerKey", "player_key", default="") or "")
    pid = str(pick(p, "playerId", "player_id", "id", default="") or "")
    if not key and ".p." in pid:
        key = pid
    pid = (pid or key).rsplit(".p.", 1)[-1]
    if not key and pid and game_key:
        key = f"{game_key}.p.{pid}"
    return key, pid


def _positions(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(x for x in re.split(r"[,/ ]+", value) if x)
    if isinstance(value, list):
        return tuple(str(_name(x) if isinstance(x, Mapping) else x) for x in value if x)
    return ()


def norm_slot(value: Any) -> str:
    if isinstance(value, Mapping):
        value = pick(value, "position", "slot", "name", default="")
    text = str(value or "").strip().upper()
    return _SLOT_ALIASES.get(text, text)


def derived_eligibility(display_position: str, status: str) -> tuple[str, ...]:
    """Eligibility from the display position when Flaim omits Yahoo's list (historical rosters): positions, their
    G/F flex slots and IL/IL+ from the injury status. FBM adds UTIL itself."""
    base = [p.upper() for p in _positions(display_position)]
    if {"PG", "SG"} & set(base):
        base.append("G")
    if {"SF", "PF"} & set(base):
        base.append("F")
    return with_il_eligibility(tuple(dict.fromkeys(base)), status)


# ------------------------------------------------------------------ leagues (get_user_session)


@dataclass(frozen=True)
class LeagueRef:
    league_key: str
    season_year: int | None
    name: str
    team_key: str
    team_name: str

    @property
    def game_key(self) -> str:
        return game_key_of(self.league_key)


def session_leagues(payload: Mapping[str, Any]) -> list[LeagueRef]:
    """The owner's Yahoo NBA leagues Flaim knows (connected at flaim.app/leagues)."""
    data = data_of(payload)
    out = []
    for lg in records(data.get("allLeagues")):
        if str(lg.get("platform") or "").lower() != PLATFORM:
            continue
        if str(lg.get("sport") or SPORT).lower() != SPORT:
            continue
        key = str(pick(lg, "leagueId", "leagueKey", default="") or "")
        if not key:
            continue
        out.append(
            LeagueRef(
                league_key=key,
                season_year=as_int(pick(lg, "seasonYear", "season")),
                name=str(pick(lg, "leagueName", "name", default="") or ""),
                team_key=team_key(key, pick(lg, "teamKey", "teamId")),
                team_name=str(pick(lg, "teamName", default="") or ""),
            )
        )
    return out


def discovery_row(ref: LeagueRef, info: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The row shape of `fbm.yahoo.discovery.list_nba_leagues`."""
    info = info or {}
    return {
        "game_key": ref.game_key,
        "season": str(ref.season_year or pick(info, "season", default="") or ""),
        "league_key": ref.league_key,
        "name": ref.name or str(pick(info, "name", default="") or ""),
        "num_teams": str(pick(info, "numTeams", "size", default="") or ""),
        "scoring_type": str(pick(info, "scoringType", default="") or ""),
        "draft_status": str(pick(info, "draftStatus", default="") or ""),
        "current_week": str(pick(info, "currentWeek", default="") or ""),
        "url": str(pick(info, "url", default="") or ""),
    }


# ------------------------------------------------------------------ league info, teams, standings


def league_team_rows(info: Mapping[str, Any], league_key: str) -> list[dict[str, Any]]:
    out = []
    for t in records(info.get("teams")):
        key = team_key(league_key, pick(t, "teamKey", "team_key", "teamId", "id"))
        if key:
            out.append({**t, "_team_key": key})
    return out


def parse_standings(payload: Mapping[str, Any], league_key: str) -> list[StandingRow]:
    rows = []
    for r in records(data_of(payload).get("standings")):
        key = team_key(league_key, pick(r, "teamKey", "team_key", "teamId", "id"))
        record = r.get("record") if isinstance(r.get("record"), Mapping) else r
        rows.append(
            StandingRow(
                team_key=key,
                name=str(pick(r, "teamName", "name", default="") or ""),
                rank=as_int(pick(r, "rank")),
                wins=as_int(pick(record, "wins")) or 0,
                losses=as_int(pick(record, "losses")) or 0,
                ties=as_int(pick(record, "ties")) or 0,
                pct=as_float(pick(record, "percentage", "winPercentage", "pct")),
                games_back=str(pick(r, "gamesBack", "games_back", default="") or ""),
                playoff_seed=as_int(pick(r, "playoffSeed", "playoff_seed")),
            )
        )
    return sorted(rows, key=lambda r: (r.rank is None, r.rank or 0))


def _week_adds(value: Any) -> int | None:
    if isinstance(value, Mapping):
        value = pick(value, "value", "count")
    return as_int(value)


def parse_teams(
    info: Mapping[str, Any], standings: Mapping[str, Any] | None, league_key: str, my_team_key: str
) -> list[YahooTeam]:
    priority = {
        team_key(league_key, pick(r, "teamKey", "team_key", "teamId", "id")): as_int(pick(r, "waiverPriority"))
        for r in records(data_of(standings or {}).get("standings"))
    }
    rows = league_team_rows(info, league_key) or league_team_rows(
        {"teams": data_of(standings or {}).get("standings")}, league_key
    )  # league info without teams: the standings list them too
    teams = []
    for t in rows:
        key = t["_team_key"]
        teams.append(
            YahooTeam(
                team_key=key,
                name=str(pick(t, "teamName", "name", default="") or ""),
                manager=str(pick(t, "ownerName", "managerName", "manager", default="") or ""),
                is_mine=key == my_team_key or bool(pick(t, "isOwnedByCurrentLogin", "isMine", "isUserTeam")),
                waiver_priority=priority.get(key) or as_int(pick(t, "waiverPriority", "waiver_priority")),
                moves=as_int(pick(t, "numberOfMoves", "moves", "number_of_moves")) or 0,
                trades=as_int(pick(t, "numberOfTrades", "trades", "number_of_trades")) or 0,
                week_adds=_week_adds(pick(t, "rosterAdds", "weekAdds", "roster_adds")),
            )
        )
    return teams


# ------------------------------------------------------------------ matchups (category rows)


def stat_id_of(row: Mapping[str, Any]) -> str | None:
    sid = pick(row, "statId", "stat_id", "statID")
    if sid is not None:
        return str(sid)
    label = pick(row, "displayName", "abbr", "abbreviation", "label", "name", "category", "_key")
    if label is None:
        return None
    text = str(label).strip().upper()
    return LABEL_STAT_IDS.get(text) or (text if text.isdigit() else None)


def raw_value(value: Any) -> str | None:
    """A stat value as Yahoo writes it ("302", "0.480", "120/250"); None when unknown."""
    if isinstance(value, Mapping):
        value = pick(value, "value", "display", "displayValue")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(int(value)) if float(value).is_integer() else str(value)
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class CategoryCell:
    stat_id: str
    value: str | None
    result: str | None  # win | loss | tie (this side's), None when unknown
    display_only: bool = False
    label: str = ""


_RESULTS = {"win": "win", "won": "win", "loss": "loss", "lost": "loss", "tie": "tie", "tied": "tie", "draw": "tie"}


def _result(value: Any) -> str | None:
    return _RESULTS.get(str(value or "").strip().lower())


def _outcome(winner: Any, side: str) -> str | None:
    """A matchup-level winner ("home" | "away" | "tie") as this side's result."""
    text = str(winner or "").strip().lower()
    if text == "tie":
        return "tie"
    return ("win" if text == side else "loss") if text in SIDES else None


def _cell(row: Mapping[str, Any], value: Any, result: Any) -> CategoryCell | None:
    sid = stat_id_of(row)
    if sid is None:
        return None
    flag = pick(row, "isDisplayOnly", "isOnlyDisplayStat", "displayOnly")
    label = str(pick(row, "displayName", "abbr", "label", "name", "_key", default="") or "")
    return CategoryCell(sid, raw_value(value), _result(result), bool(flag) or sid in DISPLAY_ONLY, label)


_ROW_FIELDS = ("categories", "categoryStats", "categoryResults", "stats", "statValues")


def side_cells(matchup: Mapping[str, Any], side: Mapping[str, Any], name: str) -> list[CategoryCell]:
    """One side's category values: rows on the side itself ({statId, value, result}) or rows on the matchup
    holding both sides ({statId, home, away, winner})."""
    for field in _ROW_FIELDS:
        rows = records(side.get(field))
        if rows:
            cells = [_cell(r, pick(r, "value", "total", "displayValue"), pick(r, "result", "outcome")) for r in rows]
            return [c for c in cells if c]
    for field in _ROW_FIELDS:
        rows = records(matchup.get(field))
        if not rows:
            continue
        cells = []
        for r in rows:
            values = r.get("values") if isinstance(r.get("values"), Mapping) else r
            outcome = _outcome(pick(r, "winner", "leader", "result"), name)
            cells.append(_cell(r, pick(values, name, f"{name}Value", f"{name}_value"), outcome))
        return [c for c in cells if c]
    return []


def _side_key(side: Mapping[str, Any], league_key: str) -> str:
    return team_key(league_key, pick(side, "teamKey", "team_key", "teamId", "id"))


def _points(side: Mapping[str, Any]) -> float | None:
    points = as_float(pick(side, "points", "totalPoints"))
    if points is not None:
        return points
    score = side.get("categoryScore")
    return as_float(pick(score, "wins")) if isinstance(score, Mapping) else None


def matchup_dates(*sources: Mapping[str, Any] | None) -> tuple[date | None, date | None]:
    """A matchup period's first and last ET day, from the first source that names them."""
    for src in sources:
        start = as_date(pick(src, "weekStart", "week_start", "startDate", "periodStart"))
        end = as_date(pick(src, "weekEnd", "week_end", "endDate", "periodEnd"))
        if start and end:
            return start, end
    return None, None


def parse_matchups(
    payload: Mapping[str, Any], league_key: str, windows: Mapping[int, tuple[date, date]] | None = None
) -> list[Matchup]:
    """Matchups with Yahoo stat ids -> raw values per team, plus stat winners. `windows` (week -> dates FBM saw
    while that week was current) fills dates Flaim doesn't send."""
    data = data_of(payload)
    week_default = as_int(pick(data, "matchupWeek", "week", "matchupPeriod", "currentWeek"))
    out = []
    for m in records(data.get("matchups")):
        sides = [(n, m.get(n)) for n in SIDES if isinstance(m.get(n), Mapping)]
        if not sides:
            sides = [(f"team{i}", t) for i, t in enumerate(records(m.get("teams")))]
        teams, winners = [], {}
        keys = [_side_key(s, league_key) for _, s in sides]
        for (name, side), key in zip(sides, keys, strict=True):
            stats = {}
            for cell in side_cells(m, side, name):
                if cell.value is not None:
                    stats[cell.stat_id] = cell.value
                other = next((k for k in keys if k != key), None)
                if cell.result is None or cell.display_only or (cell.result == "loss" and other is None):
                    continue
                winners[cell.stat_id] = None if cell.result == "tie" else (key if cell.result == "win" else other)
            teams.append(MatchupTeam(key, str(pick(side, "teamName", "name", default="") or ""), stats, _points(side)))
        week = as_int(pick(m, "week", "matchupPeriod")) or week_default
        start, end = matchup_dates(m, data)
        if start is None and week is not None and windows and week in windows:
            start, end = windows[week]
        out.append(
            Matchup(
                week=week,
                week_start=start,
                week_end=end,
                status=MATCHUP_STATUS.get(str(m.get("status") or ""), str(m.get("status") or "")),
                is_playoffs=bool(pick(m, "isPlayoffs", "is_playoffs", "playoffs")),
                teams=tuple(teams),
                stat_winners=winners,
            )
        )
    return out


def matchup_categories(payload: Mapping[str, Any]) -> list[CategoryCell]:
    """Every category row seen in the matchups (one per stat id), for the league's scoring categories."""
    seen: dict[str, CategoryCell] = {}
    for m in records(data_of(payload).get("matchups")):
        for name in SIDES:
            if isinstance(m.get(name), Mapping):
                for cell in side_cells(m, m[name], name):
                    seen.setdefault(cell.stat_id, cell)
    return list(seen.values())


def games_remaining(payload: Mapping[str, Any], league_key: str, team: str) -> int | None:
    """Yahoo's "games left in the period" for one team's side, when Flaim sends it."""
    for m in records(data_of(payload).get("matchups")):
        for name in SIDES:
            side = m.get(name)
            if isinstance(side, Mapping) and _side_key(side, league_key) == team:
                return as_int(pick(side, "gamesRemaining", "games_remaining"))
    return None


# ------------------------------------------------------------------ settings (Yahoo-shaped, for summarize())


def _roster_positions(info: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = pick(info, "rosterPositions", "roster_positions", "rosterSlots", "rosterConfiguration")
    counts: dict[str, int] = {}
    starting: dict[str, bool] = {}
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            slot = norm_slot(item)
            counts[slot] = counts.get(slot, 0) + 1
        elif isinstance(item, Mapping):
            slot = norm_slot(pick(item, "position", "slot", "name", "abbr"))
            if slot:
                counts[slot] = counts.get(slot, 0) + (as_int(pick(item, "count", "num")) or 1)
                flag = pick(item, "isStartingPosition", "is_starting_position", "isStarting", "starting")
                if flag is not None:
                    starting[slot] = flag in (True, 1, "1", "true", "True")
    if isinstance(raw, Mapping):
        for slot, value in raw.items():
            count = as_int(pick(value, "count") if isinstance(value, Mapping) else value)
            if count:
                counts[norm_slot(slot)] = count
    return [
        {
            "position": "Util" if slot == "UTIL" else slot,
            "count": str(n),
            "is_starting_position": "1" if starting.get(slot, slot not in BENCH_SLOTS) else "0",
        }
        for slot, n in counts.items()
        if slot
    ]


def _stat_rows(info: Mapping[str, Any], seen: Iterable[CategoryCell]) -> list[dict[str, str]]:
    listed = records(pick(info, "statCategories", "stat_categories", "scoringCategories", "categories"))
    out = []
    for row in listed:
        sid = stat_id_of(row)
        if sid is None:
            continue
        flag = pick(row, "isDisplayOnly", "isOnlyDisplayStat", "is_only_display_stat")
        enabled = pick(row, "enabled", "isEnabled")
        out.append(_stat(sid, str(pick(row, "displayName", "abbr", "name", default="") or ""), bool(flag), enabled))
    if not out:
        out = [_stat(c.stat_id, c.label, c.display_only, True) for c in seen]
    return out


def _stat(sid: str, label: str, display_only: bool, enabled: Any) -> dict[str, str]:
    label = label or STAT_LABELS.get(sid, sid)
    return {
        "stat_id": sid,
        "name": label,
        "display_name": label,
        "sort_order": "0" if sid in LOWER_IS_BETTER else "1",
        "enabled": "0" if enabled in (False, 0, "0", "false") else "1",
        "is_only_display_stat": "1" if display_only or sid in DISPLAY_ONLY else "0",
    }


_LEAGUE_FIELDS = {
    "league_key": ("leagueKey", "league_key"),
    "name": ("name", "leagueName"),
    "season": ("season", "seasonYear", "seasonId"),
    "num_teams": ("numTeams", "num_teams", "size", "totalRosters"),
    "current_week": ("currentWeek", "current_week", "currentMatchupPeriod"),
    "start_week": ("startWeek", "start_week"),
    "end_week": ("endWeek", "end_week"),
    "start_date": ("startDate", "start_date", "seasonStart"),
    "end_date": ("endDate", "end_date", "seasonEnd"),
    "draft_status": ("draftStatus", "draft_status"),
    "weekly_deadline": ("weeklyDeadline", "weekly_deadline"),
    "is_finished": ("isFinished", "is_finished"),
    "url": ("url",),
}
_SETTING_FIELDS = {
    "draft_type": ("draftType",),
    "scoring_type": ("scoringType", "scoringTypeRaw"),
    "uses_playoff": ("usesPlayoff",),
    "playoff_start_week": ("playoffStartWeek",),
    "num_playoff_teams": ("numPlayoffTeams",),
    "waiver_type": ("waiverType",),
    "waiver_rule": ("waiverRule",),
    "waiver_time": ("waiverTime",),
    "uses_faab": ("usesFaab",),
    "post_draft_players": ("postDraftPlayers",),
    "max_teams": ("maxTeams",),
    "max_weekly_adds": ("maxWeeklyAdds",),
    "max_adds": ("maxAdds",),
    "trade_end_date": ("tradeEndDate",),
    "cant_cut_list": ("cantCutList",),
    "can_trade_draft_picks": ("canTradeDraftPicks",),
    "weekly_deadline": ("weeklyDeadline",),
}


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def settings_raw(info: Mapping[str, Any], seen: Iterable[CategoryCell] = ()) -> dict[str, Any]:
    """League info in the shape `fbm.yahoo.settings.summarize` reads (Yahoo's settings XML, parsed)."""
    league = {k: _text(v) for k, names in _LEAGUE_FIELDS.items() if (v := pick(info, *names)) is not None}
    settings: dict[str, Any] = {
        k: _text(v) for k, names in _SETTING_FIELDS.items() if (v := pick(info, *names)) is not None
    }
    settings["roster_positions"] = {"roster_position": _roster_positions(info)}
    settings["stat_categories"] = {"stats": {"stat": _stat_rows(info, seen)}}
    return {**league, "settings": settings}


# ------------------------------------------------------------------ rosters


def parse_roster(payload: Mapping[str, Any], team: str, day: date, game_key: str) -> tuple[Roster, list[str]]:
    """A team's roster on `day`. Returns (roster, warnings about missing fields)."""
    data = data_of(payload)
    warnings: list[str] = []
    players, no_slot, no_elig, editable_rows = [], 0, 0, 0
    for p in records(pick(data, "players", "roster", "entries")):
        key, _pid = _player_key(p, game_key)
        status = str(pick(p, "status", "injuryStatus", default="") or "")
        display = ",".join(_positions(pick(p, "position", "displayPosition", "positions", default="")))
        eligible = _positions(p.get("eligiblePositions"))
        if not eligible:
            no_elig += 1
            eligible = derived_eligibility(display, status)
        selected = norm_slot(pick(p, "selectedPosition", "slot", "lineupSlot", "selected_position", "rosterSlot"))
        if not selected:
            no_slot += 1
        editable = p.get("isEditable")
        editable_rows += editable is True
        players.append(
            RosterPlayer(
                player_key=key,
                name=_name(pick(p, "name", "fullName", "playerName", default="")),
                nba_team=str(pick(p, "team", "proTeam", "editorialTeamAbbr", "nbaTeam", default="") or ""),
                display_position=display,
                eligible_positions=eligible,
                selected_position=selected or "BN",
                status=status,
                is_editable=editable is not False,
            )
        )
    if no_slot:
        warnings.append(f"roster {team} {day}: {no_slot} player(s) without a lineup slot (assumed bench)")
    if no_elig and pick(pick(data, "snapshot", default={}), "type") in (None, "current"):
        warnings.append(f"roster {team} {day}: {no_elig} player(s) without Yahoo eligibility (derived from position)")
    # Like Yahoo's roster-level flag: only my own current lineup is editable. Flaim leaves isEditable out of other
    # teams' rows and of past days (players default to editable, as in Yahoo's XML; the roster does not).
    return Roster(team_key=team, day=day, is_editable=editable_rows > 0, players=tuple(players)), warnings


# ------------------------------------------------------------------ available players


def parse_available(payload: Mapping[str, Any], game_key: str) -> list[YahooPlayer]:
    out = []
    data = data_of(payload)
    for p in records(pick(data, "freeAgents", "players")):
        key, pid = _player_key(p, game_key)
        state = str(pick(p, "acquisitionState", default="") or "")
        display = ",".join(_positions(pick(p, "position", "displayPosition", default="")))
        out.append(
            YahooPlayer(
                player_key=key,
                player_id=pid,
                name=_name(pick(p, "name", "fullName", default="")),
                nba_team=str(pick(p, "team", "proTeam", default="") or ""),
                display_position=display,
                eligible=_positions(p.get("eligiblePositions")) or _positions(display),
                status=str(pick(p, "status", "injuryStatus", default="") or ""),
                ownership_type=ACQUISITION.get(state) or str(pick(p, "ownershipType", default="") or ""),
                owner_team_key="",
                waiver_date=as_date(pick(p, "waiverDate", "waiver_date")),
                percent_owned=as_float(pick(p, "percentOwned", "percent_owned")),
                percent_owned_delta=as_float(pick(p, "percentOwnedDelta", "percentOwnedChange")),
            )
        )
    return out


# ------------------------------------------------------------------ transactions


def _stamp(row: Mapping[str, Any]) -> datetime | None:
    ms = as_float(pick(row, "timestamp"))
    if ms:
        try:
            return datetime.fromtimestamp(ms / 1000 if ms > 1e11 else ms, UTC)
        except (ValueError, OverflowError, OSError):  # a nonsense stamp: fall back to the date
            pass
    day = as_date(pick(row, "date"))
    return datetime(day.year, day.month, day.day, tzinfo=UTC) if day else None


def _tx_player(p: Mapping[str, Any], game_key: str, **legs: str) -> TxPlayer:
    key, pid = _player_key(p, game_key)
    return TxPlayer(
        player_key=key,
        player_id=pid,
        name=_name(pick(p, "name", default="")),
        nba_team=str(pick(p, "team", "proTeam", default="") or ""),
        action=legs["action"],
        source_type=str(pick(p, "source_type", "sourceType", default=legs.get("source_type", "")) or ""),
        source_team_key=legs.get("source_team_key", ""),
        destination_type=str(pick(p, "destination_type", "destinationType", default=legs.get("destination_type", ""))),
        destination_team_key=legs.get("destination_team_key", ""),
    )


def _trade_legs(row: Mapping[str, Any], league_key: str, game_key: str) -> list[TxPlayer]:
    sides = records(row.get("trade_sides") or row.get("tradeSides"))
    keys = [team_key(league_key, s.get("team_id") or s.get("teamId")) for s in sides]
    legs = []
    for side, key in zip(sides, keys, strict=True):
        other = next((k for k in keys if k != key), "")
        for p in records(side.get("acquired")):
            legs.append(
                _tx_player(
                    p,
                    game_key,
                    action="trade",
                    source_type="team",
                    source_team_key=other,
                    destination_type="team",
                    destination_team_key=key,
                )
            )
    if legs:
        return legs
    for p in [*records(row.get("players_added")), *records(row.get("players_dropped"))]:
        legs.append(_tx_player(p, game_key, action="trade", source_type="team", destination_type="team"))
    return legs


def parse_transactions(payload: Mapping[str, Any], league_key: str, my_team_key: str = "") -> list[Transaction]:
    """Newest first, as Flaim sends them. Statuses and types use Yahoo's words (successful, add/drop, ...)."""
    game_key = game_key_of(league_key)
    out = []
    for r in records(data_of(payload).get("transactions")):
        kind = str(r.get("type") or "")
        added, dropped = records(r.get("players_added")), records(r.get("players_dropped"))
        ids = r.get("team_ids") if isinstance(r.get("team_ids"), list) else []
        teams = [team_key(league_key, t) for t in ids if t not in (None, "")]
        trader = team_key(league_key, pick(r, "trader_team_key", "traderTeamKey", "proposed_by_team_id"))
        tradee = team_key(league_key, pick(r, "tradee_team_key", "tradeeTeamKey"))
        if kind in ("trade", "pending_trade"):
            legs = _trade_legs(r, league_key, game_key)
            if trader and not tradee and len(teams) == 2:
                tradee = next((t for t in teams if t != trader), "")
        else:
            pending = kind == "waiver" or r.get("status") == "pending"
            actor = teams[0] if len(teams) == 1 else (my_team_key if pending and my_team_key else "")
            claim = kind == "waiver" or pick(r, "faab_bid", "waiver_priority") is not None
            legs = [
                _tx_player(
                    p,
                    game_key,
                    action="add",
                    source_type="waivers" if claim else "freeagents",
                    destination_type="team",
                    destination_team_key=actor,
                )
                for p in added
            ] + [
                _tx_player(
                    p, game_key, action="drop", source_type="team", source_team_key=actor, destination_type="waivers"
                )
                for p in dropped
            ]
            if kind in ("add", "drop") and added and dropped:
                kind = "add/drop"
        stamp = _stamp(r)
        key = str(pick(r, "transaction_id", "transactionKey", "id", default="") or "")
        if not key:
            ids = ",".join(p.player_id for p in legs)
            key = f"flaim:{kind}:{int(stamp.timestamp()) if stamp else 0}:{ids}"
        status = str(r.get("status") or "")
        status = TX_STATUS.get(status, status)  # Flaim's words -> Yahoo's; Yahoo's own words pass through
        if kind == "pending_trade" and status in ("", "pending"):
            status = OPEN_OFFER
        out.append(
            Transaction(
                key=key,
                type=kind,
                status=status,
                timestamp=stamp,
                players=tuple(legs),
                trader_team_key=trader,
                tradee_team_key=tradee,
                trade_note=str(pick(r, "trade_note", "tradeNote", default="") or ""),
                waiver_team_key=(teams[0] if len(teams) == 1 else my_team_key) if kind == "waiver" else "",
            )
        )
    return out


# ------------------------------------------------------------------ the league, read through Flaim

WEEKS_PATH = RAW_DIR / "flaim_weeks.json"  # {league_key: {week: [start, end]}}, kept by each Flaim snapshot


def load_week_windows(league_key: str, path: Path | None = None) -> dict[int, tuple[date, date]]:
    """Week -> (first, last ET day) as FBM saw it while that week was current (Flaim may send no week dates)."""
    path = path or WEEKS_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get(league_key, {})
        return {int(k): (date.fromisoformat(v[0]), date.fromisoformat(v[1])) for k, v in raw.items()}
    except (OSError, ValueError, TypeError, IndexError, AttributeError):
        return {}


class FlaimLeague:
    """One Yahoo NBA league read through Flaim. The `fbm.yahoo` fetch functions route to its methods when handed
    one of these instead of a YahooClient (see fbm.flaim.route.league_client)."""

    is_flaim = True
    auth_mode = "flaim"

    def __init__(
        self,
        client: Any,
        league_key: str = "",
        season_year: int | None = None,
        team_key: str = "",
        week_windows: Mapping[int, tuple[date, date]] | None = None,
    ):
        self.client = client
        self.league_key = league_key
        self.season_year = season_year
        self.team_key = team_key
        self.week_windows = dict(week_windows if week_windows is not None else load_week_windows(league_key))
        self.warnings: list[str] = []
        self._cache: dict[Any, Any] = {}

    @classmethod
    def from_config(cls, client: Any = None) -> FlaimLeague:
        from fbm.config import NotConfiguredError, load_league_config

        try:
            cfg = load_league_config()
        except NotConfiguredError:
            return cls(client or FlaimClient())
        season = as_int(cfg.season)
        return cls(client or FlaimClient(), cfg.league_key, season, cfg.team_key)

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> FlaimLeague:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str) -> dict[str, Any]:
        raise FlaimUnavailable(f"The Yahoo resource `{path}`")

    # ------------------------------------------------------------------ calls

    def _call(self, tool: str, args: dict[str, Any], cache: bool = True) -> dict[str, Any]:
        key = (tool, json.dumps(args, sort_keys=True, default=str))
        if cache and key in self._cache:
            return self._cache[key]
        payload = self.client.call_tool(tool, args)
        warning = data_of(payload).get("warning") or payload.get("warning")
        if warning:
            self.warnings.append(f"{tool}: {str(warning)[:200]}")
        if cache:
            self._cache[key] = payload
        return payload

    def _args(self, league_key: str | None = None, **extra: Any) -> dict[str, Any]:
        key = league_key or self.league_key
        if not key:
            raise FlaimError("no league selected: run `fbm flaim login` (or `fbm league select`)")
        season = self.season_year if key == self.league_key and self.season_year else self._season_of(key)
        args: dict[str, Any] = {"platform": PLATFORM, "sport": SPORT, "league_id": key, "season_year": season}
        args.update({k: v for k, v in extra.items() if v is not None})
        return args

    def _season_of(self, league_key: str) -> int:
        ref = next((r for r in self.leagues_refs() if r.league_key == league_key), None)
        if ref is None or ref.season_year is None:
            raise FlaimError(f"league {league_key} isn't connected in Flaim: add it at https://flaim.app/leagues")
        return ref.season_year

    # ------------------------------------------------------------------ discovery

    def session(self) -> dict[str, Any]:
        return self._call("get_user_session", {})

    def leagues_refs(self) -> list[LeagueRef]:
        return session_leagues(self.session())

    def resolved(self, ref: LeagueRef) -> tuple[LeagueRef, dict[str, Any]]:
        """`ref` with the league's and my team's current names from league info (the session keeps the names from
        when the league was connected; a renamed team shows its old name there), plus that info. Session values
        when league info can't be read."""
        try:
            info = self.league_info(ref.league_key)
        except FlaimError as err:
            self.warnings.append(f"league info {ref.league_key}: {err.description}")
            return ref, {}
        row = next((t for t in league_team_rows(info, ref.league_key) if t["_team_key"] == ref.team_key), None)
        team_name = str(pick(row, "teamName", "name", default="") or "") if row else ""
        league_name = str(pick(info, "name", "leagueName", default="") or "")
        return replace(ref, name=league_name or ref.name, team_name=team_name or ref.team_name), info

    def leagues(self) -> list[dict[str, str]]:
        return [discovery_row(*self.resolved(r)) for r in self.leagues_refs()]

    def my_teams(self) -> list[dict[str, str]]:
        refs = [self.resolved(r)[0] for r in self.leagues_refs() if r.team_key]
        return [{"team_key": r.team_key, "name": r.team_name} for r in refs]

    # ------------------------------------------------------------------ league reads

    def league_info(self, league_key: str | None = None) -> dict[str, Any]:
        return data_of(self._call("get_league_info", self._args(league_key)))

    def standings_payload(self, league_key: str | None = None) -> dict[str, Any]:
        return self._call("get_standings", self._args(league_key))

    def matchups_payload(self, league_key: str | None = None, week: int | None = None) -> dict[str, Any]:
        return self._call("get_matchups", self._args(league_key, week=week))

    def settings_raw(self, league_key: str | None = None) -> dict[str, Any]:
        info = self.league_info(league_key)
        seen = matchup_categories(self.matchups_payload(league_key))
        raw = settings_raw(info, seen)
        if not raw["settings"]["roster_positions"]["roster_position"]:
            self.warnings.append("league info: no roster positions (FBM's default Yahoo slots assumed)")
        if not raw["settings"]["stat_categories"]["stats"]["stat"]:
            self.warnings.append("league info: no scoring categories (standard 9-cat assumed)")
        return raw

    def teams(self, league_key: str | None = None) -> list[YahooTeam]:
        key = league_key or self.league_key
        teams = parse_teams(self.league_info(key), self.standings_payload(key), key, self.team_key)
        if not teams:
            self.warnings.append("league info: no teams listed")
        return teams

    def standings(self, league_key: str | None = None) -> list[StandingRow]:
        key = league_key or self.league_key
        return parse_standings(self.standings_payload(key), key)

    def scoreboard(self, league_key: str | None = None, week: int | None = None) -> list[Matchup]:
        key = league_key or self.league_key
        return parse_matchups(self.matchups_payload(key, week), key, self.week_windows)

    def roster(self, team: str, day: date | None = None, current: bool = True) -> Roster:
        """`current` = today's roster (Flaim's current read, with eligibility); else the daily snapshot of `day`."""
        from fbm.timeutil import nba_today

        day = day or nba_today()
        league_key = team.rsplit(".t.", 1)[0] if ".t." in team else self.league_key
        as_of = None if current and day >= nba_today() else day.isoformat()
        payload = self._call("get_roster", self._args(league_key, team_id=team, as_of_date=as_of))
        roster, warnings = parse_roster(payload, team, day, game_key_of(league_key))
        self.warnings.extend(warnings)
        return roster

    def available(self, league_key: str | None = None, count: int = 100) -> list[YahooPlayer]:
        """The top `count` available players by Yahoo-wide % owned (Flaim's ordinary list holds at most 100: FBM
        always reads all 100 once per run and slices)."""
        key = league_key or self.league_key
        players = parse_available(self._call("get_free_agents", self._args(key, count=100)), game_key_of(key))
        return players[: max(0, int(count))]

    def players(self, league_key: str | None = None, status: str = "W", limit: int = 100) -> list[YahooPlayer]:
        pool = self.available(league_key, 100)
        if status == "W":
            pool = [p for p in pool if p.ownership_type == "waivers"]
        elif status == "FA":
            pool = [p for p in pool if p.ownership_type != "waivers"]
        return pool[:limit]

    def transactions(self, league_key: str | None = None, count: int = 50, start: int = 0) -> list[Transaction]:
        """The latest `count` (Flaim's rolling 30-day window, at most 100). Flaim has no offset paging, so any page
        after the first is empty; `transactions_between` reads older history."""
        if start:
            return []
        key = league_key or self.league_key
        args = self._args(key, count=max(1, min(int(count), 100)))
        return parse_transactions(self._call("get_transactions", args, cache=False), key, self.team_key)

    def transactions_between(self, start: date, end: date, league_key: str | None = None) -> list[Transaction]:
        """Completed transactions on UTC days [start, end] (at most 366 days per call)."""
        key = league_key or self.league_key
        args = self._args(key, start_date=start.isoformat(), end_date=end.isoformat())
        return parse_transactions(self._call("get_transactions", args, cache=False), key, self.team_key)

    def pending(self, league_key: str | None = None, team: str | None = None) -> list[Transaction]:
        """The owner's pending waiver claims and pending trades."""
        key = league_key or self.league_key
        mine = team or self.team_key
        out: list[Transaction] = []
        for kind in ("waiver", "pending_trade"):
            payload = self._call("get_transactions", self._args(key, type=kind), cache=False)
            out += parse_transactions(payload, key, mine)
        unknown = [t for t in out if t.type == "pending_trade" and not (t.trader_team_key and t.tradee_team_key)]
        if unknown:
            self.warnings.append(
                f"pending trades: {len(unknown)} without a known proposer (not classified incoming/outgoing)"
            )
        return out
