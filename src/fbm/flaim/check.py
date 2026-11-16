"""`fbm flaim check` and `fbm flaim dump`: read every Flaim feed FBM uses and report what works. Read-only.

Each feed is read once (FlaimLeague caches within a run), then a dry-run snapshot assembles them exactly as
`fbm snapshot` would, which surfaces mapping misses (Yahoo players without an ESPN id) and every warning about
fields Flaim didn't send. `dump` keeps each raw tool response as JSON so the mappings can be checked against real
payloads; responses carry league data only, never tokens.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fbm.analytics.categories import NINE_CAT
from fbm.config import RAW_DIR, LeagueConfig
from fbm.flaim import banked, reads
from fbm.flaim.reads import FlaimLeague

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
DUMP_DIR = RAW_DIR / "flaim"


@dataclass(frozen=True)
class Feed:
    name: str
    status: str  # OK | WARN | FAIL | SKIP
    detail: str
    used_for: str


@dataclass
class Report:
    feeds: list[Feed] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmatched: list[dict[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(f.status == FAIL for f in self.feeds)


def _leagues(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    refs = r.leagues_refs()
    names = ", ".join(f"{x.name or x.league_key} (season {x.season_year})" for x in refs)
    if not refs:
        return FAIL, "no Yahoo NBA leagues connected: add yours at https://flaim.app/leagues"
    if cfg and not any(x.league_key == cfg.league_key for x in refs):
        return WARN, f"{len(refs)} league(s): {names}; the selected {cfg.league_key} isn't among them"
    return OK, f"{len(refs)} Yahoo NBA league(s): {names}"


def _info(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    from fbm.yahoo.settings import summarize

    summary = summarize(r.settings_raw())
    cats = [c["stat_id"] for c in summary["scoring_categories"]]
    known = sum(1 for c in NINE_CAT if c.yahoo_stat_id in cats)
    slots = sum(int(s["count"]) for s in summary["roster_slots"])
    teams = len(reads.league_team_rows(r.league_info(), r.league_key))
    detail = (
        f"{teams} teams; {summary.get('scoring_type_label') or 'scoring type ?'}; {known}/9 categories mapped; "
        f"{slots or 'no'} roster slots; max weekly adds {summary['settings'].get('max_weekly_adds') or '-'}; "
        f"week {summary.get('current_week', '-')}"
    )
    return (OK if teams and known == 9 and slots else WARN), detail


def _standings(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    rows = r.standings()
    teams = r.teams()
    prio = sum(1 for t in teams if t.waiver_priority)
    mine = next((x for x in rows if x.team_key == r.team_key), None)
    you = f"; you: rank {mine.rank or '-'}, {mine.wins}-{mine.losses}-{mine.ties}" if mine else ""
    return (OK if rows else WARN), f"{len(rows)} teams, {prio} with waiver priority{you}"


def _matchups(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    matchups = r.scoreboard()
    mine = next((m for m in matchups if m.team(r.team_key)), None)
    if mine is None:
        return WARN, f"{len(matchups)} matchups; yours not found"
    stats = mine.team(r.team_key).stats if mine.team(r.team_key) else {}
    cats = sum(1 for c in NINE_CAT if c.yahoo_stat_id in stats)
    rows = {c.stat_id for c in reads.matchup_categories(r.matchups_payload())}  # rows sent, values or not
    listed = sum(1 for c in NINE_CAT if c.yahoo_stat_id in rows)
    if banked.reports_made_attempted(stats):
        ma = "Yahoo's makes/attempts sent"
    elif "9004003" in stats:
        ma = f"makes/attempts row empty ({stats['9004003']!r}: nothing banked yet, else rebuilt from box scores)"
    else:
        ma = "no makes/attempts (rebuilt from box scores)"
    dates = f"{mine.week_start}..{mine.week_end}" if mine.week_start else "no dates (estimated)"
    status = OK if cats == 9 or (listed == 9 and (cats == 0 or mine.status == "preevent")) else WARN
    detail = (
        f"{len(matchups)} matchups, week {mine.week} ({mine.status}); {listed}/9 category rows, {cats} with values "
        f"(none before the week's first games); {ma}; {dates}"
    )
    return status, detail


def _roster(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    roster = r.roster(r.team_key, day)
    elig = sum(1 for p in roster.players if p.eligible_positions)
    hurt = sum(1 for p in roster.players if p.status)
    starters = sum(1 for p in roster.players if p.selected_position not in reads.BENCH_SLOTS)
    status = OK if roster.players else FAIL
    return status, f"{len(roster.players)} players ({starters} in active slots, {elig} with eligibility, {hurt} hurt)"


def _past_roster(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    past = day - timedelta(days=1)
    roster = r.roster(r.team_key, past, current=False)
    slots = sum(1 for p in roster.players if p.selected_position)
    return (OK if roster.players else WARN), f"{len(roster.players)} players on {past} ({slots} with a lineup slot)"


def _available(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    pool = r.available(count=100)
    waivers = sum(1 for p in pool if p.ownership_type == "waivers")
    dated = sum(1 for p in pool if p.waiver_date)
    owned = sum(1 for p in pool if p.percent_owned is not None)
    detail = f"{len(pool)} available ({waivers} on waivers, {dated} with a clear date; {owned} with % owned)"
    return (OK if pool else WARN), detail


def _transactions(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    txs = r.transactions(count=25)
    placed = sum(1 for t in txs if all(p.source_team_key or p.destination_team_key for p in t.players))
    latest = f" (latest {txs[0].type} {txs[0].timestamp:%Y-%m-%d})" if txs and txs[0].timestamp else ""
    status = OK if placed == len(txs) else WARN
    return status, f"{len(txs)} in the last 30 days, {placed} with every move attributed to a team{latest}"


def _pending(r: FlaimLeague, cfg: LeagueConfig | None, day: date) -> tuple[str, str]:
    txs = r.pending()
    trades = [t for t in txs if t.type == "pending_trade"]
    unknown = sum(1 for t in trades if not (t.trader_team_key and t.tradee_team_key))
    detail = f"{len(trades)} pending trade(s) ({unknown} without a known proposer), {len(txs) - len(trades)} claim(s)"
    return (WARN if unknown else OK), detail


Check = tuple[str, str, Callable[[FlaimLeague, LeagueConfig | None, date], tuple[str, str]]]
CHECKS: tuple[Check, ...] = (
    ("Your leagues", "league selection (`fbm flaim leagues`)", _leagues),
    ("League info", "categories, roster slots, add limits", _info),
    ("Standings", "standings, waiver priority", _standings),
    ("Matchups (this week)", "matchup odds, banked weekly stats", _matchups),
    ("Your roster (today)", "lineup, IL, add/drop", _roster),
    ("Your roster (yesterday)", "banked makes/attempts, weekly retro", _past_roster),
    ("Free agents + waivers", "pickups, streaming, waiver claims", _available),
    ("Transactions", "league intel, move verification, adds used", _transactions),
    ("Pending trades & claims", "`fbm offers`", _pending),
)


def run(reader: FlaimLeague, cfg: LeagueConfig | None, day: date, conn: sqlite3.Connection | None = None) -> Report:
    report = Report()
    for name, used_for, fn in CHECKS:
        if fn is not _leagues and cfg is None:
            report.feeds.append(Feed(name, SKIP, "no league selected (`fbm flaim login`)", used_for))
            continue
        try:
            status, detail = fn(reader, cfg, day)
        except Exception as err:  # noqa: BLE001 - each feed reports on its own
            status, detail = FAIL, f"{type(err).__name__}: {str(err)[:200]}"
        report.feeds.append(Feed(name, status, detail, used_for))
    report.warnings.extend(reader.warnings)
    reader.warnings.clear()
    if cfg is not None and conn is not None:
        report.feeds.append(_dry_run(reader, cfg, day, conn, report))
    return report


def _dry_run(reader: FlaimLeague, cfg: LeagueConfig, day: date, conn: sqlite3.Connection, report: Report) -> Feed:
    from fbm.flaim.league import build_snapshot
    from fbm.yahoo.ids import IdMapper, espn_players_from_db, load_overrides

    used_for = "everything: the snapshot every tool reads"
    try:
        mapper = IdMapper(espn_players_from_db(conn), load_overrides())
        snap = build_snapshot(reader, cfg, mapper, day, conn, remembered=reader.week_windows)
    except Exception as err:  # noqa: BLE001
        return Feed("Snapshot (dry run)", FAIL, f"{type(err).__name__}: {str(err)[:200]}", used_for)
    report.warnings.extend(w for w in snap.warnings if w not in report.warnings)
    report.unmatched = [{"yahoo_id": m.yahoo_id, "name": m.name, "team": m.team} for m in snap.matches if not m.espn_id]
    lg = snap.league
    sizes = sorted(len(t.players) for t in lg.teams)
    names = {t.team_key: t.name for t in lg.teams}
    banked_txt = ", ".join(f"{names.get(k, k)}: {v}" for k, v in snap.banked_source.items()) or "none yet"
    detail = (
        f"{len(lg.teams)} teams (rosters {sizes[0] if sizes else 0}-{sizes[-1] if sizes else 0} players); "
        f"week {lg.week} {lg.week_start}..{lg.week_end} ({snap.week_source}); opponent "
        f"{names.get(lg.opponent_key or '', '-')}; banked: {banked_txt}; {len(report.unmatched)} players without an "
        "ESPN id"
    )
    return Feed("Snapshot (dry run)", WARN if report.unmatched else OK, detail, used_for)


# ------------------------------------------------------------------ raw dump


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")[:60]


class Recorder:
    """Saves every tool response to `out_dir` as it arrives (args + payload; no tokens are ever in either)."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.paths: list[Path] = []

    def __call__(self, tool: str, arguments: dict[str, Any], payload: dict[str, Any]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        extra = [f"{k}-{v}" for k, v in sorted(arguments.items()) if k not in ("platform", "sport", "season_year")]
        name = f"{len(self.paths) + 1:02d}_{tool}" + (f"_{_slug('_'.join(extra))}" if extra else "") + ".json"
        path = self.out_dir / name
        path.write_text(json.dumps({"tool": tool, "arguments": arguments, "payload": payload}, indent=1), "utf-8")
        self.paths.append(path)


def dump_dir(base: Path | None = None, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return (base or DUMP_DIR) / stamp
