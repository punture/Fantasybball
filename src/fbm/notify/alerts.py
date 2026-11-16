"""Deterministic owner alerts (no AI) behind `fbm alert lineup` and `fbm alert injuries`.

Pure functions decide and render from engine outputs; the thin collectors at the bottom read FBM's services. Alerts
fire only when something is actionable and new (dedupe state in the notify_state table, keyed per ET game day for
lineups). FBM never changes the league: lineup alerts carry the exact manual steps for Yahoo.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from fbm.league.model import IL_SLOTS, INACTIVE_SLOTS, LeagueState, RosterPlayer, can_fill
from fbm.notify import state as notify_state
from fbm.notify.text import clip, clock, day_label, fit, until
from fbm.timeutil import nba_today

LINEUP_KEY = "alert.lineup"
INJURY_KEY = "alert.injuries"
LINEUP_WINDOW = timedelta(minutes=90)  # alert when the first affected tip-off is this close
INJURY_REFRESH_MIN = 10  # re-read ESPN injuries when older than this
SNAPSHOT_MAX_AGE_MIN = 10  # re-read Yahoo before alerting from an older snapshot
SEEN_SIGNALS_KEPT = 300
ALERT_LIMIT = 3500

DOUBTFUL = frozenset({"GTD", "DTD", "Q", "D", "QUESTIONABLE", "DOUBTFUL", "DAY-TO-DAY"})
OUT = frozenset({"O", "OUT", "OFS", "INJ", "SUSP", "SUSPENSION", "INJURED RESERVE", "NA", "OUT FOR SEASON"})
BACK = frozenset({"CLEARED", "ACTIVE", "HEALTHY", "PROBABLE", ""})


class AlertError(RuntimeError):
    pass


# ---------------------------------------------------------------- lineup (pure)


@dataclass(frozen=True)
class Flag:
    player_id: str
    name: str
    status: str
    slot: str
    tip: datetime
    options: tuple[str, ...] = ()  # bench players with a game tonight who can fill his slot


@dataclass(frozen=True)
class LineupFindings:
    changes: tuple[tuple[str, str, str, str], ...]  # (player_id, name, from slot, to slot)
    out_starters: tuple[Flag, ...]
    doubtful_starters: tuple[Flag, ...]
    first_tip: datetime
    starts_before: int
    starts_after: int

    @property
    def key(self) -> str:
        """Dedupe key: the recommended moves and flagged starters (not times or counts)."""
        raw = json.dumps(
            [
                sorted([pid, a, b] for pid, _n, a, b in self.changes),
                sorted(f.player_id for f in self.out_starters),
                sorted([f.player_id, f.status.upper()] for f in self.doubtful_starters),
            ]
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def tipoffs(games: Iterable[Mapping[str, Any]], now: datetime) -> tuple[set[str], dict[str, datetime]]:
    """(NBA teams playing on the day, team -> tip-off of its game if it hasn't started)."""
    playing: set[str] = set()
    upcoming: dict[str, datetime] = {}
    for g in games:
        teams = (str(g["home"]), str(g["away"]))
        playing.update(teams)
        start = datetime.fromisoformat(str(g["start_utc"]))
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        if g["state"] not in ("in", "post") and start > now:
            for t in teams:
                upcoming[t] = min(start, upcoming.get(t, start))
    return playing, upcoming


def find_lineup_issues(
    players: Sequence[RosterPlayer],
    assignments: Mapping[str, str],
    starting_slots: Collection[str],
    playing: Collection[str],
    upcoming: Mapping[str, datetime],
    now: datetime,
    window: timedelta = LINEUP_WINDOW,
) -> LineupFindings | None:
    """What needs the owner before tip-off: recommended changes that involve a player who plays tonight, an Out
    starter whose team plays, a GTD/DTD starter with a game (check before tip). None unless the first such tip-off
    is within `window` (and still ahead)."""
    starting = set(starting_slots)
    after = {p.player_id: assignments.get(p.player_id, p.selected) for p in players}

    def tip(p: RosterPlayer) -> datetime | None:  # tip-off if he can still play tonight
        return None if p.is_out else upcoming.get(p.nba_team)

    def plays(p: RosterPlayer) -> bool:
        return p.nba_team in playing and not p.is_out

    changes = tuple(
        (p.player_id, p.name, p.selected, after[p.player_id]) for p in players if after[p.player_id] != p.selected
    )
    moved = {c[0] for c in changes}
    deadlines = [t for p in players if p.player_id in moved and (t := tip(p))]
    out_flags = tuple(
        Flag(p.player_id, p.name, p.status, p.selected, t)
        for p in players
        if p.selected in starting and p.is_out and (t := upcoming.get(p.nba_team))
    )
    bench = [p for p in players if after[p.player_id] == "BN" and tip(p)]
    doubtful = tuple(
        Flag(p.player_id, p.name, p.status, slot, t, tuple(b.name for b in bench if can_fill(b.eligible, slot)))
        for p in players
        if (slot := after[p.player_id]) in starting and p.status.upper() in DOUBTFUL and (t := tip(p))
    )
    times = deadlines + [f.tip for f in (*out_flags, *doubtful)]
    if not times or min(times) - now > window:
        return None
    return LineupFindings(
        changes,
        out_flags,
        doubtful,
        min(times),
        sum(1 for p in players if p.selected in starting and plays(p)),
        sum(1 for p in players if after[p.player_id] in starting and plays(p)),
    )


def render_lineup_alert(f: LineupFindings, day: date, now: datetime, tz: tzinfo, blocked: Sequence[str] = ()) -> str:
    lines = [
        f"**Lineup alert · {day_label(day)}**",
        f"First tip-off {clock(f.first_tip, tz)} ({until(f.first_tip, now)}) · starters with a game: "
        f"{f.starts_before} → {f.starts_after}",
    ]
    moved = {c[0] for c in f.changes}
    for x in f.out_starters:
        fix = "" if x.player_id in moved else " No bench player with a game can take his spot: see `fbm stream`."
        lines.append(f"- **{x.name}** is {x.status} and in your lineup ({x.slot}, game {clock(x.tip, tz)}).{fix}")
    for x in f.doubtful_starters:
        backup = f" If he sits, start {' or '.join(x.options[:2])}." if x.options else ""
        lines.append(f"- **{x.name}** is {x.status} (game {clock(x.tip, tz)}): check he plays before tip-off.{backup}")
    lines += [f"- Needs a roster move first (proposal): {b}" for b in blocked]
    if f.changes:
        lines += [
            "",
            "**Set in Yahoo** (FBM never changes your league):",
            f"Open Yahoo Fantasy → My Team → date **{day.isoformat()}** → Edit lineup:",
            *(f"- {name}: {a} → {b}" for _pid, name, a, b in f.changes),
            "Save. Optional: `fbm lineup --done` once it's set.",
        ]
    return fit(lines, [], ALERT_LIMIT, "")


# ---------------------------------------------------------------- injuries (pure)


@dataclass(frozen=True)
class StatusChange:
    player_id: str
    name: str
    team: str
    old: str
    new: str
    comment: str
    side: str  # mine | opp
    slot: str
    game_today: bool


def status_class(status: str | None) -> str:
    s = (status or "").strip().upper()
    if s in OUT:
        return "out"
    if s in DOUBTFUL:
        return "doubtful"
    return "back" if s in BACK else "other"


def latest_changes(
    events: Iterable[Mapping[str, Any]],
    mine: Mapping[str, RosterPlayer],
    opp: Mapping[str, RosterPlayer],
    playing: Collection[str],
) -> list[StatusChange]:
    """The newest status change per player on my roster or my opponent's (events in id order)."""
    latest: dict[str, StatusChange] = {}
    for e in events:
        pid = str(e["espn_id"])
        side, roster = ("mine", mine) if pid in mine else ("opp", opp) if pid in opp else ("", None)
        if roster is None:
            continue
        p = roster[pid]
        first = latest.get(pid)
        latest[pid] = StatusChange(
            pid,
            p.name,
            p.nba_team,
            first.old if first else str(e["old_status"] or ""),
            str(e["new_status"] or ""),
            str(e["comment"] or ""),
            side,
            p.selected,
            p.nba_team in playing,
        )
    return [c for c in latest.values() if c.old.upper() != c.new.upper()]


def what_to_do(c: StatusChange) -> str:
    kind = status_class(c.new)
    if c.side == "opp":
        return {
            "out": "their production drops: your matchup odds improve (`fbm sim`)",
            "back": "back for them: expect more production on their side",
        }.get(kind, "monitor")
    active = c.slot not in INACTIVE_SLOTS
    if kind == "out":
        if c.slot in IL_SLOTS:
            return "already on IL: nothing to do"
        if c.game_today and active:
            return "bench him tonight (or IL if Yahoo allows it); the lineup alert has the exact steps"
        return "move him to IL if Yahoo allows it, to free a roster spot"
    if kind == "doubtful":
        return "game-time call tonight: keep a bench backup with a game" if c.game_today and active else "monitor"
    if kind == "back":
        return "activate him from IL" if c.slot in IL_SLOTS else "available again: start him on game days"
    return "check his status before tip-off"


_SIGNAL_LABEL = {"opportunity": "next man up", "role_change": "role change"}


def render_injury_alert(
    when: str, changes: Sequence[StatusChange], opponent: str | None, pickups: Sequence[Mapping[str, Any]]
) -> str | None:
    def line(c: StatusChange) -> str:
        note = f"\n  ESPN: {clip(c.comment, 100)}" if c.comment else ""  # ESPN's note: data, quoted as-is
        return f"- **{c.name}** ({c.team}) {c.old or '?'} → **{c.new}**: {what_to_do(c)}.{note}"

    mine = [line(c) for c in changes if c.side == "mine"][:8]
    theirs = [line(c) for c in changes if c.side == "opp"][:6]
    picks = [
        f"- **{s['player']}** ({s.get('nba') or '?'}) · {_SIGNAL_LABEL.get(s['kind'], s['kind'])}: "
        f'{clip(s.get("note"), 110)} → pickup candidate: `fbm waiver "{s["player"]}"`'
        for s in pickups[:3]
    ]
    if not (mine or theirs or picks):
        return None
    sections = [
        ["**Your team**", *mine] if mine else [],
        [f"**Opponent · {clip(opponent, 40) or 'this week'}**", *theirs] if theirs else [],  # team name: untrusted
        ["**Next man up (free agents)**", *picks] if picks else [],
    ]
    return fit([f"**Injury update · {when}**"], sections, ALERT_LIMIT, "*{n} more section(s) cut.*")


# ---------------------------------------------------------------- collectors (read FBM's services)


def refresh_injuries(
    conn: sqlite3.Connection,
    now: datetime,
    max_age_min: float = INJURY_REFRESH_MIN,
    client_factory: Callable | None = None,
) -> str | None:
    """Re-read ESPN's injury report when it is older than `max_age_min` (records status changes). Returns a note
    when the refresh failed (the alert then runs on the last report)."""
    row = conn.execute("SELECT last_run FROM sync_log WHERE job = 'injuries'").fetchone()
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        if now - (last if last.tzinfo else last.replace(tzinfo=UTC)) < timedelta(minutes=max_age_min):
            return None
    from fbm.data import sync
    from fbm.data.espn import EspnClient

    try:
        sync.sync_injuries(conn, (client_factory or EspnClient)())
    except Exception as err:  # noqa: BLE001 - stale injuries beat no alert
        return f"ESPN injuries not refreshed ({type(err).__name__})"
    return None


def _games(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT home, away, start_utc, state FROM games WHERE et_date = ? AND season_type = 2", (day.isoformat(),)
    ).fetchall()


def _snapshot_age_min(league: LeagueState, now: datetime) -> float | None:
    if not league.fetched_at:
        return None
    fetched = datetime.fromisoformat(league.fetched_at)
    return (now - (fetched if fetched.tzinfo else fetched.replace(tzinfo=UTC))).total_seconds() / 60


def _my_tip_is_near(conn: sqlite3.Connection, league: LeagueState, now: datetime) -> bool:
    """Does any of my players tip off within LINEUP_WINDOW (and not yet)?"""
    _, upcoming = tipoffs(_games(conn, nba_today(now)), now)
    tips = [upcoming[p.nba_team] for p in league.my_team.players if p.nba_team in upcoming]
    return bool(tips) and min(tips) - now <= LINEUP_WINDOW


def _lineup_findings(svc: Any, conn: sqlite3.Connection, league: LeagueState, now: datetime):
    day = nba_today(now)
    playing, upcoming = tipoffs(_games(conn, day), now)
    if not _my_tip_is_near(conn, league, now):
        return None  # nothing of mine tips off soon: skip the optimizer
    plan = svc._lineup_plan(league, day, True, None, False)
    findings = find_lineup_issues(
        league.my_team.players, plan["res"].assignments, league.starting_slots(), playing, upcoming, now
    )
    blocked = [f"{m.name}: {m.reason}" for m in plan["moves"] if m.blocked]
    return (findings, blocked) if findings else None


def lineup_alert(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> tuple[str | None, dict[str, Any] | None]:
    """(alert text, dedupe state to save once it is out) or (None, None) when nothing is due."""
    from fbm.gm import services as svc

    refresh_injuries(conn, now)
    if (blocked := svc._ht.gate()) is not None:
        raise AlertError(blocked.get("message") or "Suggestions are paused (Hashtag unhealthy).")
    league = svc.active_league()
    if not _my_tip_is_near(conn, league, now):
        return None, None  # nothing of mine tips off soon: no Yahoo read, no optimizer
    age = _snapshot_age_min(league, now)
    if league.source == "yahoo" and age is not None and age > SNAPSHOT_MAX_AGE_MIN:
        # Yahoo statuses (O / GTD) and the lineup the owner has set are only as fresh as the snapshot (served up to
        # an hour): before a tip-off, re-read it so a starter ruled out since then is seen in time.
        from fbm.league.yahoo_league import load_or_refresh

        league = svc._roll_week(load_or_refresh(SNAPSHOT_MAX_AGE_MIN))
    found = _lineup_findings(svc, conn, league, now)
    if not found:
        return None, None
    findings, blocked_moves = found
    day = nba_today(now)
    saved = notify_state.get(conn, LINEUP_KEY) or {}
    if saved.get("et_date") == day.isoformat() and saved.get("key") == findings.key:
        return None, None
    return render_lineup_alert(findings, day, now, tz, blocked_moves), {"et_date": day.isoformat(), "key": findings.key}


def injury_alert(conn: sqlite3.Connection, now: datetime, tz: tzinfo) -> tuple[str | None, dict[str, Any]]:
    """(alert text or None, new dedupe state). Always save the state (it moves the event watermark)."""
    from fbm.gm import services as svc

    refresh_injuries(conn, now)
    saved = notify_state.get(conn, INJURY_KEY)
    if saved is None:  # first run: the current report and signal feed are the baseline; later changes are news
        last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM injury_events").fetchone()[0]
        return None, {"last_event_id": int(last), "signals": sorted(int(s["signal_id"]) for s in _pickup_feed(svc))}
    since = int(saved.get("last_event_id", 0))
    events = conn.execute("SELECT * FROM injury_events WHERE id > ? ORDER BY id", (since,)).fetchall()
    league = svc.active_league()
    try:
        opp = svc._default_opponent(league)
    except Exception:  # noqa: BLE001 - opponent unknown (week rollover): my roster only
        opp = None
    playing, _ = tipoffs(_games(conn, nba_today(now)), now)
    changes = latest_changes(
        events,
        {p.player_id: p for p in league.my_team.players},
        {p.player_id: p for p in opp.players} if opp else {},
        playing,
    )
    if any(status_class(e["new_status"]) == "out" for e in events):
        try:
            svc.post_sync()  # re-detect opportunity signals (key teammate out)
        except Exception:  # noqa: BLE001 - the feed is additive
            pass
    feed = _pickup_feed(svc)
    seen = {int(x) for x in saved.get("signals", [])}
    pickups = [s for s in feed if int(s["signal_id"]) not in seen and s.get("direction") == "up"]
    new_state = {
        "last_event_id": max([since, *(int(e["id"]) for e in events)]),
        "signals": sorted(seen | {int(s["signal_id"]) for s in feed})[-SEEN_SIGNALS_KEPT:],
    }
    return render_injury_alert(clock(now, tz), changes, opp.name if opp else None, pickups), new_state


def _pickup_feed(svc: Any) -> list[dict[str, Any]]:
    """Free agents with an opportunity (key teammate out) or role-change signal."""
    try:
        return list(svc.signals_tool(scope="fa", kind="opportunity,role_change", top=20)["signals"])
    except Exception:  # noqa: BLE001 - no pickups section rather than no alert
        return []
