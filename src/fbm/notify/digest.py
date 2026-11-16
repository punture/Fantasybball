"""The deterministic daily / weekly digest behind `fbm digest` (no AI): a compact Telegram-ready brief, verdict
first, every number taken from FBM's engine (fbm.gm.services). Each section is collected on its own, so one
failing tool shows a short "unavailable" note instead of killing the digest. The verdict opens with old (a failed Yahoo
refresh) or unreadable league data, and never says "no moves needed" when the lineup or streaming check failed.

daily: today's actions (lineup, matchup odds, injuries, best stream, top waiver target, pickups, your queue, health).
weekly (Monday): the new matchup preview (opponent, per-category odds, games both sides, the week's streaming plan,
adds available, pickups, queue, health).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Any

from fbm.notify.text import clip, clock, day_label, fit, pct, zone
from fbm.settings import Settings, load_settings
from fbm.timeutil import nba_today

DIGEST_LIMIT = 3500
ODDS_SIMS = 5000
KINDS = ("daily", "weekly")
WAITING = {
    "pending_approval": "needs your decision: `fbm proposals approve {ref}` or `reject {ref}`",
    "approved": "approved: get the steps with `fbm proposals steps {ref}`",
    "awaiting_owner": "make the move in Yahoo, then `fbm proposals done {ref}`",
}


@dataclass(frozen=True)
class Unavailable:
    reason: str


def attempt(fn: Callable[[], Any]) -> Any:
    """Run one section's tool; failures (exceptions or {"error": ...} payloads) become Unavailable."""
    try:
        out = fn()
    except Exception as err:  # noqa: BLE001 - one failing section never kills the digest
        return Unavailable(clip(f"{type(err).__name__}: {err}", 120))
    if isinstance(out, dict) and out.get("error"):
        return Unavailable(clip(out.get("message") or out["error"], 120))
    return out


def _ok(value: Any) -> bool:
    return value is not None and not isinstance(value, Unavailable)


# ---------------------------------------------------------------- collection (FBM services)


def _first_tip(svc: Any, now: datetime) -> datetime | None:
    from fbm.league.model import IL_SLOTS
    from fbm.notify.alerts import tipoffs

    league = svc.active_league()
    rows = (
        svc._conn()
        .execute(
            "SELECT home, away, start_utc, state FROM games WHERE et_date = ? AND season_type = 2",
            (nba_today(now).isoformat(),),
        )
        .fetchall()
    )
    _, upcoming = tipoffs(rows, now)
    tips = [
        upcoming[p.nba_team] for p in league.my_team.players if p.selected not in IL_SLOTS and p.nba_team in upcoming
    ]
    return min(tips) if tips else None


def _snapshot(svc: Any, now: datetime) -> dict[str, Any] | Unavailable | None:
    """The league's data freshness: alerts.stale_snapshot when a Yahoo snapshot couldn't be re-read, Unavailable
    (the whole message, which carries the fix) when the league can't be read at all, else None."""
    from fbm.constants import C
    from fbm.notify.alerts import stale_snapshot

    try:
        league = svc.active_league()
    except Exception as err:  # noqa: BLE001 - reported in the verdict
        return Unavailable(clip(err, 200))
    return stale_snapshot(league, now, float(C("yahoo.snapshot_max_age_min")))


def _injuries(svc: Any) -> dict[str, Any]:
    league = svc.active_league()
    try:
        opp = svc._default_opponent(league)
    except Exception:  # noqa: BLE001 - opponent unknown (week rollover): my roster only
        opp = None
    slots = {p.player_id: p.selected for p in league.my_team.players}
    theirs = set(opp.player_ids()) if opp else set()
    rows = svc.injuries("league")["injuries"]
    return {
        "opponent": opp.name if opp else None,
        "mine": [{**r, "slot": slots[r["espn_id"]]} for r in rows if r["espn_id"] in slots],
        "opp": [r for r in rows if r["espn_id"] in theirs],
    }


def _top_target(svc: Any, radar: Any) -> dict[str, Any] | None:
    if not _ok(radar) or not radar.get("players"):
        return None
    top = radar["players"][0]
    if not top.get("beats_worst_rostered"):
        return None
    drop = (top.get("suggested_drop") or {}).get("id")
    return svc.waiver_decision(top["id"], drop)


def collect(kind: str = "daily", record: bool = False, now: datetime | None = None, svc: Any = None) -> dict[str, Any]:
    """Every section's engine output (or Unavailable). record=True (scheduled --send runs) logs the lineup and
    streaming decisions under the daily-gm / weekly-plan trigger; previews log them as owner-ask."""
    if svc is None:
        from fbm.gm import services as svc
    now = now or datetime.now(UTC)
    weekly = kind == "weekly"
    trigger = ("weekly-plan" if weekly else "daily-gm") if record else "owner-ask"
    d: dict[str, Any] = {"kind": kind, "now": now, "today_et": nba_today(now)}
    d["snapshot"] = attempt(lambda: _snapshot(svc, now))
    d["overview"] = attempt(svc.league_overview)
    d["odds"] = attempt(lambda: svc.simulate_matchup_tool(n=ODDS_SIMS))
    if not weekly:
        d["lineup"] = attempt(lambda: svc.lineup_steps(trigger=trigger))
        d["first_tip"] = attempt(lambda: _first_tip(svc, now))
    d["injuries"] = attempt(lambda: _injuries(svc))
    d["stream"] = attempt(lambda: svc.streaming_plan(trigger=trigger))
    d["radar"] = attempt(lambda: svc.fa_radar_tool(top=3))
    if not weekly:
        d["waiver"] = attempt(lambda: _top_target(svc, d["radar"]))
    d["signals"] = attempt(lambda: svc.signals_tool(scope="fa", kind="opportunity,role_change", new_only=True, top=3))
    d["proposals"] = attempt(lambda: svc.list_proposals("open"))
    d["offers"] = attempt(svc.pending_offers_tool)
    d["health"] = attempt(svc.health_check)
    return d


# ---------------------------------------------------------------- rendering (pure)


def _unavailable(title: str, value: Unavailable) -> list[str]:
    return [f"**{title}**: unavailable ({value.reason})"]


def _team(name: Any) -> str:
    """A fantasy team name: managers choose them, so they are untrusted data and never format or link."""
    return clip(name, 40) or "?"


def _guarded(title: str, render: Callable[[], list[str]]) -> list[str]:
    """One section's lines; an unexpected engine output shape costs that section only, never the digest."""
    try:
        return render()
    except Exception as err:  # noqa: BLE001 - one bad section never kills the digest
        return _unavailable(title, Unavailable(f"{type(err).__name__} while rendering"))


def _data_note(d: Mapping[str, Any], tz: tzinfo) -> str:
    """The verdict's first words when the league data is old (a failed Yahoo refresh) or couldn't be read at all."""
    from fbm.notify.alerts import render_stale

    snap = d.get("snapshot")
    if isinstance(snap, Unavailable):
        return f"FBM couldn't read your league ({snap.reason})."
    return render_stale(snap, tz) if snap else ""


def _verdict(d: Mapping[str, Any], tz: tzinfo) -> str:
    odds, weekly = d.get("odds"), d["kind"] == "weekly"
    if _ok(odds):
        p = float(odds["p_win_majority"])
        label = "Favored" if p >= 0.6 else "Underdog" if p < 0.4 else "Toss-up"
        head = (
            f"{label} vs {_team(odds['opponent'])}: **{pct(p)}** to win the week "
            f"({float(odds['expected_cats_won']):.1f} of {len(odds['categories'])} cats expected)."
        )
    else:
        head = "Matchup odds unavailable."
    todo, later = [], []
    lineup = d.get("lineup")
    if _ok(lineup) and lineup.get("changes"):
        todo.append(f"{len(lineup['changes'])} lineup change(s)")
    stream = d.get("stream")
    if _ok(stream) and stream.get("moves"):
        moves = stream["moves"]
        gain = sum(float(m["gain_expected_cats"]) for m in moves) if weekly else float(moves[0]["gain_expected_cats"])
        if weekly:
            todo.append(f"{len(moves)} stream(s) (+{gain:.2f} cats)")
        else:
            effective = _effective_day(moves[0])
            if effective is not None and effective > d["today_et"]:
                # The plan times the add after the dropped player's last useful game: doing it today would cost games.
                later.append(f"stream {moves[0]['add']} on {day_label(effective)}, not before (+{gain:.2f} cats)")
            else:
                todo.append(f"stream {moves[0]['add']} (+{gain:.2f} cats)")
    waiting = _waiting(d.get("proposals"))
    if waiting:
        todo.append(f"{len(waiting)} proposal(s) need you")
    offers = d.get("offers")
    if _ok(offers) and offers.get("incoming_trades"):
        todo.append(f"{len(offers['incoming_trades'])} trade offer(s)")
    # "No moves needed" rests on the lineup and streaming sections: when one couldn't be computed, FBM doesn't know.
    unchecked = [
        name for name, key in (("lineup", "lineup"), ("streaming", "stream")) if isinstance(d.get(key), Unavailable)
    ]
    when = "this week" if weekly else "today"
    act = f"{when.capitalize()}: {', '.join(todo)}." if todo else ""
    if unchecked:
        act += f" {' and '.join(unchecked).capitalize()} unavailable: see below."
    elif not todo:
        act = f"No moves needed {when}."
    if later:
        act += f" Next: {', '.join(later)}."
    return "**Verdict:** " + " ".join(part for part in (_data_note(d, tz), head, act.strip()) if part)


def _effective_day(move: Mapping[str, Any]) -> date | None:
    try:
        return date.fromisoformat(str(move.get("effective_et") or ""))
    except ValueError:
        return None


def _header(d: Mapping[str, Any], tz: tzinfo) -> list[str]:
    kind = "weekly preview" if d["kind"] == "weekly" else "daily"
    lines = [f"**FBM {kind} · {day_label(d['today_et'])}**"]
    ov = d.get("overview")
    if _ok(ov):
        week = ov.get("fantasy_week") or {}
        odds, inj = d.get("odds"), d.get("injuries")
        opponent = (
            ov.get("this_week_opponent")
            or (odds["opponent"] if _ok(odds) else None)
            or (inj["opponent"] if _ok(inj) else None)
        )
        bits = [f"{_team(ov['my_team'])} vs {_team(opponent)}"]
        if week.get("number"):
            bits.append(f"week {week['number']}")
        if week.get("start") and week.get("end"):
            bits.append(f"{week['start']} to {week['end']} (ET)")
        rank = ov.get("my_standing") or {}
        if rank.get("rank"):
            bits.append(f"#{rank['rank']} ({rank.get('wins', 0)}-{rank.get('losses', 0)}-{rank.get('ties', 0)})")
        if ov.get("source") == "demo":
            bits.append("demo league")
        lines.append(" · ".join(bits))
    lines.append(_verdict(d, tz))
    return lines


def _lineup(d: Mapping[str, Any], tz: tzinfo) -> list[str]:
    lineup, tip = d.get("lineup"), d.get("first_tip")
    if not _ok(lineup):
        return _unavailable("Lineup today", lineup) if isinstance(lineup, Unavailable) else []
    title = "**Lineup today**" + (f" (first tip {clock(tip, tz)})" if _ok(tip) and tip else "")
    if not lineup.get("changes"):
        lines = [title, "- Already set: nothing to change."]
    else:
        lines = [
            title,
            f"Set in Yahoo → My Team → **{lineup['date_et']}** → Edit lineup:",
            *(f"- {c}" for c in lineup["changes"][:10]),
            "Then (optional) `fbm lineup --done`.",
        ]
    lines += [f"- Needs a roster move (proposal): {b}" for b in lineup.get("il_moves_needing_a_roster_move", [])[:3]]
    return lines


def _matchup(d: Mapping[str, Any]) -> list[str]:
    odds = d.get("odds")
    if not _ok(odds):
        return _unavailable("Matchup", odds) if isinstance(odds, Unavailable) else []
    cats = sorted(odds["categories"], key=lambda c: -float(c["p_win"]))
    games = odds.get("games_started") or {}
    lines = [f"**Matchup vs {_team(odds['opponent'])}** ({odds.get('window_et', '')} ET)"]
    if d["kind"] == "weekly":
        lines.append("- " + " · ".join(f"{c['cat']} {pct(c['p_win'])}" for c in cats))
    else:
        groups = (
            ("Ahead", [c for c in cats if c["p_win"] >= 0.6]),
            ("Toss-up", [c for c in cats if 0.4 <= c["p_win"] < 0.6]),
            ("Behind", [c for c in cats if c["p_win"] < 0.4]),
        )
        lines += [f"- {name}: " + ", ".join(f"{c['cat']} {pct(c['p_win'])}" for c in cs) for name, cs in groups if cs]
    if games:
        lines.append(f"- Games left this week: you {games.get('me')} · them {games.get('opp')}")
    return lines


def _injury_line(r: Mapping[str, Any]) -> str:
    back = f"back ~{str(r['return_date'])[:10]}" if r.get("return_date") else ""
    extra = [x for x in (clip(r.get("injury_type"), 40), back) if x]
    il = " · on IL" if r.get("slot") in ("IL", "IL+") else ""
    return f"**{r.get('name')}** {clip(r.get('status'), 30)}" + (f" ({', '.join(extra)})" if extra else "") + il


def _injury_section(d: Mapping[str, Any]) -> list[str]:
    inj = d.get("injuries")
    if not _ok(inj):
        return _unavailable("Injuries", inj) if isinstance(inj, Unavailable) else []
    if not inj["mine"] and not inj["opp"]:
        return ["**Injuries**", "- None on either roster."]
    lines = ["**Injuries**"]
    lines += [f"- Yours: {_injury_line(r)}" for r in inj["mine"][:4]]
    lines += [f"- {_team(inj['opponent'])}: {_injury_line(r)}" for r in inj["opp"][:4]]
    return lines


def _move_line(m: Mapping[str, Any]) -> str:
    drop = f", drop **{m['drop']}**" if m.get("drop") else ""
    line = (
        f"- Add **{m['add']}** ({m['add_games']} games){drop} from {m['effective_et']}: "
        f"**+{float(m['gain_expected_cats']):.2f}** expected cats"
    )
    if m.get("via_waivers"):
        verdict = (m.get("waiver_verdict") or {}).get("action")
        line += f" · on waivers until {m.get('waivers_clear_et')}" + (f" ({verdict})" if verdict else "")
    return line


def _stream(d: Mapping[str, Any]) -> list[str]:
    stream = d.get("stream")
    weekly = d["kind"] == "weekly"
    title = "Streaming plan" if weekly else "Best stream"
    if not _ok(stream):
        return _unavailable(title, stream) if isinstance(stream, Unavailable) else []
    lines = [f"**{title}**"]
    if stream.get("weekly_add_limit") is not None:
        lines[0] += f" (adds left this week: {stream.get('adds_available')})"
    moves = stream.get("moves") or []
    if not moves:
        return [*lines, "- No add/drop beats standing pat."]
    lines += [_move_line(m) for m in (moves[:4] if weekly else moves[:1])]
    if weekly and len(moves) == stream.get("planner_moves_considered", len(moves)):
        before, after = stream.get("baseline") or {}, stream.get("after_plan") or {}
        if "expected_cats" in before and "expected_cats" in after:
            lines.append(
                f"- Expected cats {before['expected_cats']:.1f} → {after['expected_cats']:.1f}; win the week "
                f"{pct(before['p_win_majority'])} → {pct(after['p_win_majority'])}"
            )
    return lines


def _target(d: Mapping[str, Any]) -> list[str]:
    w = d.get("waiver")
    if isinstance(w, Unavailable):
        return _unavailable("Top waiver target", w)
    if not w:
        return []
    return [
        "**Top waiver target**",
        f"- **{w['player']}** (drop {w['drop']}): {str(w['action']).upper()}: {clip(w['reason'], 160)}",
    ]


def _pickups(d: Mapping[str, Any]) -> list[str]:
    radar, signals = d.get("radar"), d.get("signals")
    lines = []
    seen = set()
    if _ok(radar):
        for p in radar.get("players", [])[:3]:
            seen.add(p["player"])
            why = "; ".join(p.get("reasons") or [])
            lines.append(f"- **{p['player']}** ({p.get('nba')}, {p.get('confidence')}): {clip(why, 120)}")
    if _ok(signals):
        for s in signals.get("signals", [])[:3]:
            if s["player"] not in seen:
                lines.append(
                    f"- **{s['player']}** ({s.get('nba')}) {s['kind'].replace('_', ' ')}: {clip(s.get('note'), 110)}"
                )
    if not lines:
        reasons = [v.reason for v in (radar, signals) if isinstance(v, Unavailable)]
        return [f"**Pickup radar**: unavailable ({reasons[0]})"] if reasons else []
    return ["**Pickup radar**", *lines]


def _waiting(props: Any) -> list[Mapping[str, Any]]:
    if not _ok(props):
        return []
    return [p for p in props.get("proposals", []) if p.get("status") in WAITING]


def _queue(d: Mapping[str, Any]) -> list[str]:
    lines = [
        f"- {p['ref']} {clip(p.get('title'), 60)}: " + WAITING[p["status"]].format(ref=p["ref"])
        for p in _waiting(d.get("proposals"))[:4]
    ]
    offers = d.get("offers")
    if _ok(offers) and offers.get("available", True):
        for t in offers.get("incoming_trades", [])[:3]:
            verdict = (t.get("evaluation") or {}).get("verdict")
            lines.append(
                f"- Trade offer from {_team(t.get('trader') or 'another manager')}"
                + (f": FBM says {verdict}" if verdict else "")
                + " → accept or reject in Yahoo"
            )
        for c in offers.get("waiver_claims", [])[:3]:
            names = ", ".join(f"{'+' if p.get('action') == 'add' else '-'}{p['player']}" for p in c.get("players", []))
            lines.append(f"- Pending waiver claim: {names}")
    return ["**Your queue**", *lines] if lines else []


def _health(d: Mapping[str, Any]) -> list[str]:
    health = d.get("health")
    if not _ok(health):
        return _unavailable("Health", health) if isinstance(health, Unavailable) else []
    bad = [c for c in health.get("checks", []) if c.get("status") in ("WARN", "FAIL")]
    bad.sort(key=lambda c: c["status"] != "FAIL")
    return ["**Health**", *(f"- {c['status']} {c['check']}: {clip(c['detail'], 100)}" for c in bad[:3])] if bad else []


def render(d: Mapping[str, Any], settings: Settings | None = None, limit: int = DIGEST_LIMIT) -> str:
    """The digest text (FBM's Markdown subset, at most `limit` characters)."""
    tz = zone((settings or load_settings()).owner_timezone)
    weekly = d["kind"] == "weekly"
    if weekly:
        parts: Sequence[tuple[str, Callable[[], list[str]]]] = (
            ("Matchup", lambda: _matchup(d)),
            ("Streaming plan", lambda: _stream(d)),
            ("Injuries", lambda: _injury_section(d)),
            ("Pickup radar", lambda: _pickups(d)),
            ("Your queue", lambda: _queue(d)),
            ("Health", lambda: _health(d)),
        )
    else:
        parts = (
            ("Lineup today", lambda: _lineup(d, tz)),
            ("Matchup", lambda: _matchup(d)),
            ("Injuries", lambda: _injury_section(d)),
            ("Best stream", lambda: _stream(d)),
            ("Top waiver target", lambda: _target(d)),
            ("Your queue", lambda: _queue(d)),
            ("Pickup radar", lambda: _pickups(d)),
            ("Health", lambda: _health(d)),
        )
    sections = [_guarded(title, render) for title, render in parts]
    try:
        head = _header(d, tz)
    except Exception as err:  # noqa: BLE001 - still deliver the sections
        kind = "weekly preview" if weekly else "daily"
        head = [f"**FBM {kind} · {day_label(d['today_et'])}**", f"**Verdict:** unavailable ({type(err).__name__})."]
    return fit(head, sections, limit, "*{n} more section(s) cut to fit: run `fbm digest` for the full brief.*")


def build(
    kind: str = "daily", record: bool = False, now: datetime | None = None, settings: Settings | None = None
) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return render(collect(kind, record, now), settings)


def subject(kind: str, today: date) -> str:
    return f"{'Weekly preview' if kind == 'weekly' else 'Daily brief'} · {day_label(today)}"
