"""`fbm alert lineup|injuries`: when an alert is due (tip-off window), what it says, NO_REPLY and persisted dedupe."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from fbm.cli import app
from fbm.gm import services as svc
from fbm.league.model import FantasyTeam, LeagueState, RosterPlayer
from fbm.notify import alerts
from fbm.notify import state as notify_state
from fbm.store import db

CHI = ZoneInfo("America/Chicago")
DAY = date(2026, 11, 16)
TIP_BOS = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)  # 7:00 PM ET / 6:00 PM CST
TIP_NYK = datetime(2026, 11, 17, 0, 30, tzinfo=UTC)
TIP_LAL = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)
SLOTS = ["PG", "SG", "UTIL"]


def P(pid, team, slot, status="", elig=("PG", "SG", "G", "UTIL")):
    return RosterPlayer(pid, pid.upper(), team, elig, slot, status)


def _roster():
    return [
        P("a", "BOS", "PG", "O"),  # starter ruled out, game tonight
        P("b", "NYK", "BN"),  # bench, game tonight
        P("c", "LAL", "SG", "GTD"),  # starter, game-time decision
        P("d", "MIA", "UTIL"),  # starter, no game
        P("e", "NYK", "BN"),
    ]


PLAYING = {"BOS", "NYK", "LAL", "PHI"}
UPCOMING = {"BOS": TIP_BOS, "NYK": TIP_NYK, "LAL": TIP_LAL}


def test_tipoffs_skip_started_games():
    now = datetime(2026, 11, 17, 0, 10, tzinfo=UTC)
    games = [
        {"home": "BOS", "away": "PHI", "start_utc": TIP_BOS.isoformat(), "state": "pre"},  # tip time passed
        {"home": "NYK", "away": "TOR", "start_utc": TIP_NYK.isoformat(), "state": "pre"},
        {"home": "LAL", "away": "SAC", "start_utc": "2026-11-17T03:00:00", "state": "in"},  # live (naive = UTC)
    ]
    playing, upcoming = alerts.tipoffs(games, now)
    assert playing == {"BOS", "PHI", "NYK", "TOR", "LAL", "SAC"}
    assert upcoming == {"NYK": TIP_NYK, "TOR": TIP_NYK}


def test_lineup_findings_out_gtd_and_bench_swap_within_the_window():
    now = TIP_BOS - timedelta(minutes=60)
    recommended = {"a": "BN", "b": "PG", "c": "SG", "d": "UTIL", "e": "BN"}
    f = alerts.find_lineup_issues(_roster(), recommended, SLOTS, PLAYING, UPCOMING, now)
    assert f is not None and f.first_tip == TIP_BOS
    assert f.changes == (("a", "A", "PG", "BN"), ("b", "B", "BN", "PG"))
    assert [x.player_id for x in f.out_starters] == ["a"]
    assert [(x.player_id, x.options) for x in f.doubtful_starters] == [("c", ("E",))]
    assert (f.starts_before, f.starts_after) == (1, 2)
    # Too early: the first affected tip-off is 3 hours away.
    assert (
        alerts.find_lineup_issues(_roster(), recommended, SLOTS, PLAYING, UPCOMING, TIP_BOS - timedelta(hours=3))
        is None
    )


def test_no_alert_for_cosmetic_moves_or_started_games():
    roster = [P("d", "MIA", "UTIL"), P("f", "CHA", "BN"), P("g", "BOS", "PG")]
    swap = {"d": "BN", "f": "UTIL", "g": "PG"}  # neither moved player plays tonight
    now = TIP_BOS - timedelta(minutes=30)
    assert alerts.find_lineup_issues(roster, swap, SLOTS, PLAYING, UPCOMING, now) is None
    roster = [P("a", "BOS", "PG", "O"), P("b", "NYK", "BN")]
    started = {"NYK": TIP_NYK}  # BOS already tipped: its slot is locked, nothing to flag for it
    assert alerts.find_lineup_issues(roster, {"a": "PG", "b": "BN"}, SLOTS, PLAYING, started, now) is None


def test_dedupe_key_tracks_moves_and_flags_not_times():
    rec = {"a": "BN", "b": "PG", "c": "SG", "d": "UTIL", "e": "BN"}
    early = alerts.find_lineup_issues(_roster(), rec, SLOTS, PLAYING, UPCOMING, TIP_BOS - timedelta(minutes=80))
    late = alerts.find_lineup_issues(_roster(), rec, SLOTS, PLAYING, UPCOMING, TIP_BOS - timedelta(minutes=10))
    assert early.key == late.key
    roster = _roster()
    roster[2].status = "O"  # the GTD starter is ruled out: a new situation
    changed = alerts.find_lineup_issues(roster, rec, SLOTS, PLAYING, UPCOMING, TIP_BOS - timedelta(minutes=10))
    assert changed.key != early.key


def test_render_lineup_alert_has_owner_times_and_exact_steps():
    rec = {"a": "BN", "b": "PG", "c": "SG", "d": "UTIL", "e": "BN"}
    now = TIP_BOS - timedelta(minutes=52)
    f = alerts.find_lineup_issues(_roster(), rec, SLOTS, PLAYING, UPCOMING, now)
    text = alerts.render_lineup_alert(f, DAY, now, CHI, ["Z: healthy but the roster is full"])
    assert text.splitlines()[:2] == [
        "**Lineup alert · Mon Nov 16**",
        "First tip-off 6:00 PM CST (in 52 min) · starters with a game: 1 → 2",
    ]
    assert "- **A** is O and in your lineup (PG, game 6:00 PM CST)." in text
    assert "- **C** is GTD (game 9:00 PM CST): check he plays before tip-off. If he sits, start E." in text
    assert "- Needs a roster move first (proposal): Z: healthy but the roster is full" in text
    assert "Open Yahoo Fantasy → My Team → date **2026-11-16** → Edit lineup:\n- A: PG → BN\n- B: BN → PG" in text
    stuck = alerts.find_lineup_issues([P("a", "BOS", "PG", "O")], {"a": "PG"}, SLOTS, PLAYING, UPCOMING, now)
    assert "No bench player with a game can take his spot" in alerts.render_lineup_alert(stuck, DAY, now, CHI)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _game(conn, team, tip, day=DAY):
    conn.execute(
        "INSERT INTO games VALUES (?, 2027, 2, ?, ?, ?, 'ZZZ', 'pre', 0, 0, 1, 0, 'x')",
        (f"{team}{day}", tip.isoformat(), day.isoformat(), team),
    )


def _league(players, opp_players=()):
    me = FantasyTeam("t1", "Me", players=list(players))
    them = FantasyTeam("t2", "Them", players=list(opp_players))
    return LeagueState("l", "L", 2027, "t1", [me, them], source="demo", opponent_key="t2")


@pytest.fixture
def world(conn, monkeypatch):
    """The demo-like league behind the services: my roster, opponent, tonight's games, a stub lineup plan."""
    for team, tip in (("BOS", TIP_BOS), ("NYK", TIP_NYK), ("LAL", TIP_LAL)):
        _game(conn, team, tip)
    w = SimpleNamespace(
        league=_league(_roster(), [P("o1", "PHX", "PG"), P("o2", "DEN", "BN")]),
        plan={"a": "BN", "b": "PG", "c": "SG", "d": "UTIL", "e": "BN"},
        conn=conn,
    )
    monkeypatch.setattr(alerts, "refresh_injuries", lambda conn, now: None)
    monkeypatch.setattr(svc, "active_league", lambda: w.league)
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr(
        svc,
        "_lineup_plan",
        lambda league, day, il, trig, log: {"res": SimpleNamespace(assignments=dict(w.plan)), "moves": []},
    )
    return w


def test_lineup_alert_is_sent_once_per_recommendation_and_day(world):
    now = TIP_BOS - timedelta(minutes=45)
    text, state = alerts.lineup_alert(world.conn, now, CHI)
    assert text.startswith("**Lineup alert · Mon Nov 16**") and state["et_date"] == "2026-11-16"
    notify_state.put(world.conn, alerts.LINEUP_KEY, state)
    assert alerts.lineup_alert(world.conn, now + timedelta(minutes=15), CHI) == (None, None)
    world.league.my_team.players[2].status = "O"  # news: the GTD starter is out
    world.plan = {"a": "BN", "b": "PG", "c": "BN", "d": "UTIL", "e": "SG"}
    text2, state2 = alerts.lineup_alert(world.conn, now + timedelta(minutes=30), CHI)
    assert text2 and state2["key"] != state["key"] and "- E: BN → SG" in text2


def test_lineup_alert_skips_the_optimizer_when_no_tip_is_near(world, monkeypatch):
    monkeypatch.setattr(svc, "_lineup_plan", lambda *a: pytest.fail("optimizer should not run"))
    assert alerts.lineup_alert(world.conn, TIP_BOS - timedelta(hours=5), CHI) == (None, None)


def test_yahoo_lineup_alert_rereads_a_stale_snapshot_before_a_tip(world, monkeypatch):
    """Yahoo statuses come from the snapshot (served up to an hour): a starter ruled out since the last read must
    still be caught before tip-off, so a stale snapshot is re-read whenever one of my tip-offs is near."""
    from fbm.league import yahoo_league

    now = TIP_BOS - timedelta(minutes=45)
    stale = _league([P("a", "BOS", "PG"), P("b", "NYK", "BN")])  # healthy an hour ago: nothing to say
    stale.source, stale.fetched_at = "yahoo", (now - timedelta(minutes=50)).isoformat()
    fresh = _league([P("a", "BOS", "PG", "O"), P("b", "NYK", "BN")])  # ruled out since
    fresh.source, fresh.fetched_at = "yahoo", now.isoformat()
    reads = []
    monkeypatch.setattr(svc, "active_league", lambda: stale)
    monkeypatch.setattr(yahoo_league, "load_or_refresh", lambda age: reads.append(age) or fresh)
    monkeypatch.setattr(svc, "_roll_week", lambda league: league)

    def plan(league, day, il, trig, log):  # the optimizer only benches A once he is ruled out
        out = league.my_team.players[0].is_out
        return {
            "res": SimpleNamespace(assignments={"a": "BN", "b": "PG"} if out else {"a": "PG", "b": "BN"}),
            "moves": [],
        }

    monkeypatch.setattr(svc, "_lineup_plan", plan)
    text, state = alerts.lineup_alert(world.conn, now, CHI)
    assert reads == [alerts.SNAPSHOT_MAX_AGE_MIN] and state is not None
    assert "**A** is O and in your lineup" in text and "- B: BN → PG" in text
    assert "Yahoo data is from" not in text  # re-read: fresh data, no warning
    reads.clear()
    assert alerts.lineup_alert(world.conn, TIP_BOS - timedelta(hours=4), CHI) == (None, None)
    assert reads == []  # no tip-off near: no Yahoo read


REFRESH_ERROR = "FlaimLoginRequired: the Flaim sign-in expired or was revoked: run `fbm flaim login`"
STALE_WHY = (
    "(refresh failed: FlaimLoginRequired: the Flaim sign-in expired or was revoked: run 'fbm flaim login'): check "
    "Yahoo before acting; fix: `fbm flaim login`, then `fbm flaim check`."
)


def _yahoo_unreadable(monkeypatch, league, fetched_at):
    """A Yahoo league whose refresh fails: load_or_refresh falls back to the saved snapshot and records why."""
    from fbm.flaim import route
    from fbm.league import yahoo_league

    league.source, league.fetched_at = "yahoo", fetched_at.isoformat()
    reads = []
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_roll_week", lambda lg: lg)
    monkeypatch.setattr(yahoo_league, "load_or_refresh", lambda age: reads.append(age) or league)
    monkeypatch.setattr(yahoo_league, "last_refresh_error", lambda: REFRESH_ERROR)
    monkeypatch.setattr(route, "is_flaim", lambda settings=None: True)
    return reads


def test_lineup_alert_from_an_old_snapshot_says_so_under_the_title(world, monkeypatch):
    """Yahoo couldn't be re-read before the tip: the advice from the saved snapshot still goes out, but it says how
    old the roster and statuses are, why, and the fix."""
    now = TIP_BOS - timedelta(minutes=45)
    reads = _yahoo_unreadable(monkeypatch, world.league, now - timedelta(hours=3))
    text, state = alerts.lineup_alert(world.conn, now, CHI)
    assert reads == [alerts.SNAPSHOT_MAX_AGE_MIN] and state["key"] != alerts.STALE_KEY
    lines = text.splitlines()
    assert lines[0] == "**Lineup alert · Mon Nov 16**"
    assert lines[1] == f"**Yahoo data is from Mon Nov 16, 2:15 PM CST** {STALE_WHY}"
    assert lines[2].startswith("First tip-off 6:00 PM CST") and "- B: BN → PG" in text


def test_lineup_alert_after_a_real_failed_refresh(world, monkeypatch, tmp_path):
    """Through the real load_or_refresh: the Flaim sign-in lapsed, the saved snapshot (2.4 days old) is served, and
    the alert says so instead of advising from it."""
    from fbm.flaim import route
    from fbm.flaim.errors import FlaimLoginRequired
    from fbm.league import yahoo_league

    def lapsed(day=None):
        raise FlaimLoginRequired("the Flaim sign-in expired or was revoked")

    now = TIP_BOS - timedelta(minutes=45)
    world.league.source, world.league.fetched_at = "yahoo", (now - timedelta(days=2, hours=10)).isoformat()
    monkeypatch.setattr(yahoo_league, "SNAPSHOT_PATH", tmp_path / "yahoo_league.json")
    yahoo_league.save_league(world.league, yahoo_league.SNAPSHOT_PATH)
    monkeypatch.setattr(yahoo_league, "_last_failure", {"at": None, "error": ""})
    monkeypatch.setattr(yahoo_league, "refresh", lapsed)
    monkeypatch.setattr(route, "is_flaim", lambda settings=None: True)
    monkeypatch.setattr(svc, "active_league", lambda: yahoo_league.load_or_refresh(60))
    monkeypatch.setattr(svc, "_roll_week", lambda lg: lg)
    text, state = alerts.lineup_alert(world.conn, now, CHI)
    assert state["key"] == alerts.STALE_KEY and "→" not in text
    assert text.splitlines()[1] == (
        "**Yahoo data is from Sat Nov 14, 7:15 AM CST** (refresh failed: FlaimLoginRequired: Flaim: the Flaim sign-in "
        "expired or was revoked: run 'fbm flaim login'): check Yahoo before acting; fix: `fbm flaim login`, then "
        "`fbm flaim check`."
    )


def test_lineup_alert_gives_no_advice_from_a_snapshot_days_old(world, monkeypatch):
    """Days-old rosters (sign-in lapsed): the owner may have changed the team since, so only the warning goes out,
    once per game day; the advice resumes once Yahoo is readable again."""
    now = TIP_BOS - timedelta(minutes=45)
    _yahoo_unreadable(monkeypatch, world.league, now - timedelta(days=2, hours=10))
    plan = svc._lineup_plan
    monkeypatch.setattr(svc, "_lineup_plan", lambda *a: pytest.fail("no advice from a roster days old"))
    text, state = alerts.lineup_alert(world.conn, now, CHI)
    assert state == {"et_date": "2026-11-16", "key": alerts.STALE_KEY}
    assert text.splitlines() == [
        "**Lineup alert · Mon Nov 16**",
        f"**Yahoo data is from Sat Nov 14, 7:15 AM CST** {STALE_WHY}",
        "First tip-off 6:00 PM CST (in 45 min): FBM can't see your current lineup, so it has no advice. Check your "
        "lineup in Yahoo before tip-off.",
    ]
    notify_state.put(world.conn, alerts.LINEUP_KEY, state)
    assert alerts.lineup_alert(world.conn, now + timedelta(minutes=15), CHI) == (None, None)

    monkeypatch.setattr(svc, "_lineup_plan", plan)
    world.league.fetched_at = (now + timedelta(minutes=30)).isoformat()  # signed in again: a fresh read
    text, state = alerts.lineup_alert(world.conn, now + timedelta(minutes=30), CHI)
    assert state["key"] != alerts.STALE_KEY and "Yahoo data is from" not in text and "- B: BN → PG" in text


def test_stale_snapshot_only_for_an_old_yahoo_snapshot(monkeypatch):
    from fbm.flaim import route
    from fbm.league import yahoo_league

    monkeypatch.setattr(yahoo_league, "last_refresh_error", lambda: "")
    monkeypatch.setattr(route, "is_flaim", lambda settings=None: False)
    now = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)
    league = _league([P("a", "BOS", "PG")])
    league.fetched_at = (now - timedelta(days=3)).isoformat()
    assert alerts.stale_snapshot(league, now, 10) is None  # the demo league has no Yahoo snapshot
    league.source, league.fetched_at = "yahoo", (now - timedelta(minutes=10)).isoformat()
    assert alerts.stale_snapshot(league, now, 10) is None
    league.fetched_at = (now - timedelta(minutes=11)).replace(tzinfo=None).isoformat()  # naive = UTC
    stale = alerts.stale_snapshot(league, now, 10)
    assert stale == {"fetched_at": league.fetched_at, "age_min": 11, "refresh_error": "", "fix": "`fbm yahoo check`"}
    assert alerts.render_stale(stale, CHI) == (
        "**Yahoo data is from Mon Nov 16, 3:49 PM CST** (not refreshed): check Yahoo before acting; fix: "
        "`fbm yahoo check`."
    )
    league.fetched_at = ""
    assert alerts.stale_snapshot(league, now, 10)["age_min"] is None


def _event(conn, pid, old, new, comment=""):
    conn.execute(
        "INSERT INTO injury_events(espn_id, name, team, old_status, new_status, comment, observed_at) "
        "VALUES (?, ?, 'X', ?, ?, ?, ?)",
        (pid, pid.upper(), old, new, comment, datetime.now(UTC).isoformat()),
    )


def test_latest_change_per_player_and_what_to_do():
    mine = {p.player_id: p for p in _roster()}
    mine["il"] = P("il", "SAC", "IL")
    opp = {"o1": P("o1", "PHX", "PG")}
    events = [
        {"espn_id": "a", "old_status": "Healthy", "new_status": "Day-To-Day", "comment": ""},
        {"espn_id": "a", "old_status": "Day-To-Day", "new_status": "Out", "comment": "ankle"},
        {"espn_id": "c", "old_status": "Healthy", "new_status": "Out", "comment": ""},
        {"espn_id": "c", "old_status": "Out", "new_status": "Healthy", "comment": ""},  # net: no change
        {"espn_id": "b", "old_status": "Healthy", "new_status": "Out", "comment": ""},
        {"espn_id": "il", "old_status": "Out", "new_status": "Cleared", "comment": ""},
        {"espn_id": "o1", "old_status": "Healthy", "new_status": "Out", "comment": ""},
        {"espn_id": "zz", "old_status": "Healthy", "new_status": "Out", "comment": ""},  # not on either roster
    ]
    changes = {c.player_id: c for c in alerts.latest_changes(events, mine, opp, PLAYING)}
    assert set(changes) == {"a", "b", "il", "o1"}
    assert (changes["a"].old, changes["a"].new, changes["a"].comment) == ("Healthy", "Out", "ankle")
    assert alerts.what_to_do(changes["a"]).startswith("bench him tonight")  # active slot, game tonight
    assert alerts.what_to_do(changes["b"]).startswith("move him to IL")  # already on the bench
    assert alerts.what_to_do(changes["il"]) == "activate him from IL"
    assert "odds improve" in alerts.what_to_do(changes["o1"])
    gtd = alerts.StatusChange("c", "C", "LAL", "Healthy", "Questionable", "", "mine", "SG", True)
    assert alerts.what_to_do(gtd).startswith("game-time call")


def test_injury_alert_baseline_then_new_changes_and_pickups(world, monkeypatch):
    conn = world.conn
    _event(conn, "a", "Healthy", "Out")  # before the first run: baseline, not news
    feed = [{"signal_id": 1, "kind": "opportunity", "direction": "up", "player": "Old Pick", "nba": "OKC", "note": "x"}]
    synced = []
    monkeypatch.setattr(svc, "_default_opponent", lambda league: league.team("t2"))
    monkeypatch.setattr(svc, "signals_tool", lambda **k: {"signals": list(feed)})
    monkeypatch.setattr(svc, "post_sync", lambda: synced.append(1))
    now = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)

    text, state = alerts.injury_alert(conn, now, CHI)
    assert text is None and state == {"last_event_id": 1, "signals": [1]}
    notify_state.put(conn, alerts.INJURY_KEY, state)

    _event(conn, "c", "Healthy", "Questionable", "[game-time](https://x.y) call *tonight*")
    _event(conn, "o1", "Healthy", "Out")
    _event(conn, "zz", "Healthy", "Out")  # elsewhere in the league: triggers the signal re-scan only
    feed.append(
        {"signal_id": 2, "kind": "opportunity", "direction": "up", "player": "New Guy", "nba": "PHX", "note": "o1 out"}
    )
    feed.append(
        {"signal_id": 3, "kind": "role_change", "direction": "down", "player": "Faller", "nba": "X", "note": ""}
    )
    text, state = alerts.injury_alert(conn, now, CHI)
    assert synced == [1] and state == {"last_event_id": 4, "signals": [1, 2, 3]}
    assert text.startswith("**Injury update · 4:00 PM CST**")
    assert "- **C** (LAL) Healthy → **Questionable**: game-time call tonight: keep a bench backup with a game." in text
    assert "  ESPN: (game-time)(https://x.y) call tonight" in text  # quoted data can't format or link
    assert "**Opponent · Them**\n- **O1** (PHX) Healthy → **Out**: their production drops" in text
    assert '- **New Guy** (PHX) · next man up: o1 out → pickup candidate: `fbm waiver "New Guy"`' in text
    assert "Old Pick" not in text and "Faller" not in text
    notify_state.put(conn, alerts.INJURY_KEY, state)
    assert alerts.injury_alert(conn, now, CHI)[0] is None


def test_injury_alert_from_an_old_snapshot_says_so(world, monkeypatch):
    """ESPN's news is current, but whose player it is and his slot come from the Yahoo snapshot: say how old."""
    now = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)
    _yahoo_unreadable(monkeypatch, world.league, now - timedelta(hours=5))
    monkeypatch.setattr(svc, "_default_opponent", lambda league: league.team("t2"))
    monkeypatch.setattr(svc, "signals_tool", lambda **k: {"signals": []})
    monkeypatch.setattr(svc, "post_sync", lambda: None)
    notify_state.put(world.conn, alerts.INJURY_KEY, {"last_event_id": 0, "signals": []})
    assert alerts.injury_alert(world.conn, now, CHI)[0] is None  # an old snapshot alone is no injury news
    _event(world.conn, "c", "Healthy", "Out")
    text, _state = alerts.injury_alert(world.conn, now, CHI)
    lines = text.splitlines()
    assert lines[0] == "**Injury update · 4:00 PM CST**"
    assert lines[1] == f"**Yahoo data is from Mon Nov 16, 11:00 AM CST** {STALE_WHY}"
    assert "- **C** (LAL) Healthy → **Out**" in text


def test_refresh_injuries_only_when_stale(conn, monkeypatch):
    calls = []
    monkeypatch.setattr("fbm.data.sync.sync_injuries", lambda c, client: calls.append(client))
    now = datetime(2026, 11, 16, 22, 0, tzinfo=UTC)
    db.log_sync(conn, "injuries", "x")
    conn.execute("UPDATE sync_log SET last_run = ?", ((now - timedelta(minutes=5)).isoformat(),))
    assert alerts.refresh_injuries(conn, now, client_factory=lambda: "espn") is None and calls == []
    conn.execute("UPDATE sync_log SET last_run = ?", ((now - timedelta(minutes=30)).isoformat(),))
    assert alerts.refresh_injuries(conn, now, client_factory=lambda: "espn") is None and calls == ["espn"]

    def offline():
        raise OSError("offline")

    assert "not refreshed" in alerts.refresh_injuries(conn, now, client_factory=offline)


@pytest.fixture
def cli_db(conn, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **k: conn)
    return conn


def test_cli_alerts_print_exactly_no_reply(cli_db, monkeypatch):
    monkeypatch.setattr(alerts, "lineup_alert", lambda conn, now, tz: (None, None))
    monkeypatch.setattr(alerts, "injury_alert", lambda conn, now, tz: (None, {"last_event_id": 9, "signals": []}))
    runner = CliRunner()
    for cmd in (["alert", "lineup"], ["alert", "injuries"]):
        out = runner.invoke(app, cmd)
        assert out.exit_code == 0 and out.stdout == "NO_REPLY\n"
    assert notify_state.get(cli_db, alerts.INJURY_KEY) == {"last_event_id": 9, "signals": []}  # watermark moves


def test_cli_alert_sends_and_records_only_after_delivery(cli_db, monkeypatch):
    from fbm.notify import dispatch
    from fbm.notify.base import NotifyError

    monkeypatch.setattr(
        alerts, "lineup_alert", lambda conn, now, tz: ("**Lineup alert**", {"et_date": "d", "key": "k"})
    )
    sent = []
    monkeypatch.setattr(dispatch, "send", lambda *a, **k: sent.append((a, k)) or {"status": "sent"})
    out = CliRunner().invoke(app, ["alert", "lineup", "--send"])
    assert out.exit_code == 0 and out.stdout == "**Lineup alert**\n"
    assert sent == [(("Lineup alert", "**Lineup alert**", "alert"), {"title": False})]
    assert notify_state.get(cli_db, alerts.LINEUP_KEY) == {"et_date": "d", "key": "k"}

    notify_state.put(cli_db, alerts.LINEUP_KEY, {})

    def down(*a, **k):
        raise NotifyError("Notification not delivered: telegram: Telegram unreachable (ConnectError)")

    monkeypatch.setattr(dispatch, "send", down)
    out = CliRunner().invoke(app, ["alert", "lineup", "--send"])
    assert out.exit_code == 1 and "Telegram unreachable" in out.stderr
    assert notify_state.get(cli_db, alerts.LINEUP_KEY) == {}  # not recorded: the next run tries again


def test_cli_alert_errors_go_to_stderr(cli_db, monkeypatch):
    def broken(conn, now, tz):
        raise svc.GMError("Demo league not built. Run `fbm demo build`.")

    monkeypatch.setattr(alerts, "injury_alert", broken)
    out = CliRunner().invoke(app, ["alert", "injuries"])
    assert out.exit_code == 1 and out.stdout == "" and "Demo league not built" in out.stderr


def test_no_reply_with_send_releases_held_messages(cli_db, monkeypatch):
    from fbm.notify import dispatch

    released = []
    monkeypatch.setattr(alerts, "lineup_alert", lambda conn, now, tz: (None, None))
    monkeypatch.setattr(dispatch, "flush_held", lambda: released.append(1) or [])
    out = CliRunner().invoke(app, ["alert", "lineup", "--send"])
    assert out.exit_code == 0 and out.stdout == "NO_REPLY\n" and released == [1]


def test_paused_suggestions_are_a_quiet_no_reply(world, cli_db, monkeypatch):
    monkeypatch.setattr(svc._ht, "gate", lambda conn=None: {"error": "HT_UNHEALTHY", "message": "Hashtag is stale"})
    with pytest.raises(alerts.AlertError, match="Hashtag is stale"):
        alerts.lineup_alert(world.conn, TIP_BOS - timedelta(minutes=30), CHI)
    out = CliRunner().invoke(app, ["alert", "lineup"])
    assert out.exit_code == 0 and out.stdout == "NO_REPLY\n" and "Hashtag is stale" in out.stderr
