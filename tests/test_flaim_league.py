"""The Flaim league snapshot: same LeagueState semantics as the Yahoo XML path, plus the week window, adds used and
the banked makes/attempts rebuild Flaim needs. Offline: fixtures + an in-memory DB."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from fbm.analytics.categories import NINE_CAT
from fbm.config import LeagueConfig
from fbm.flaim import banked
from fbm.flaim import league as flaim_league
from fbm.flaim.reads import FlaimLeague
from fbm.league.model import LeagueState
from fbm.store import db
from fbm.yahoo.ids import EspnPlayer, IdMapper, espn_team
from fbm.yahoo.league_reads import Transaction, TxPlayer
from tests.test_flaim_reads import DAY, LK, ME, OPP, FakeFlaim, fixture, reader

CFG = LeagueConfig("466", "2026", LK, "Hardwood Classic", ME, "Glass Cleaners")
NOW = datetime(2026, 11, 18, 19, 0, tzinfo=UTC)
TEAMS = ("IND", "PHX", "NY", "MEM", "TOR", "SAC", "BKN", "BOS", "OKC", "DET", "MIN", "ATL", "LAL", "MIL", "DEN")
STATS = ("pts", "reb", "ast", "stl", "blk", "tov", "tpm", "fgm", "fga", "ftm", "fta")


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("fbm.timeutil.nba_today", lambda now=None: DAY)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    for d in range(16, 23):  # every team plays every day of week 5 (a busy, ordinary week)
        day = f"2026-11-{d}"
        for i in range(0, len(TEAMS) - 1, 2):
            state = "post" if d < 18 else "pre"
            c.execute(
                "INSERT INTO games(event_id, season, season_type, start_utc, et_date, home, away, state, completed, "
                "neutral, time_valid, box_synced, updated_at) VALUES (?,2027,2,?,?,?,?,?,?,0,1,1,'x')",
                (f"g{d}{i}", f"{day}T23:00:00+00:00", day, TEAMS[i], TEAMS[i + 1], state, int(d < 18)),
            )
    yield c
    c.close()


def line(c, day: str, pid: str, **stats) -> None:
    event = next(r[0] for r in c.execute("SELECT event_id FROM games WHERE et_date = ? LIMIT 1", (day,)))
    vals = [stats.get(s, 0) for s in STATS]
    c.execute(
        f"INSERT INTO box_lines(event_id, espn_id, et_date, season, team, starter, played, dnp_reason, "
        f"{', '.join(STATS)}) VALUES (?,?,?,2027,'X',1,1,'',{','.join('?' * len(STATS))})",
        (event, pid, day, *vals),
    )


def mapper() -> IdMapper:
    people = []
    for name in ("get_roster_mine", "get_roster_opponent"):
        for p in fixture(name)["data"]["players"]:
            people.append(EspnPlayer(f"e{p['playerId']}", p["name"], espn_team(p["team"])))
    return IdMapper(people, {})


def week_lines(c) -> None:
    line(c, "2026-11-16", "e6001", fgm=7, fga=15, ftm=3, fta=4, pts=20)  # Haliburton, PG
    line(c, "2026-11-16", "e6010", fgm=9, fga=17, ftm=0, fta=0, pts=20)  # J. Williams: benched that day
    line(c, "2026-11-17", "e6002", fgm=10, fga=20, ftm=6, fta=7, pts=28)  # Booker, SG
    line(c, "2026-11-17", "e6010", fgm=6, fga=12, ftm=2, fta=2, pts=15)  # J. Williams, UTIL
    line(c, "2026-11-17", "e6014", fgm=5, fga=9, ftm=1, fta=1, pts=11)  # Porzingis on IL: never counts
    line(c, "2026-11-16", "e7007", fgm=11, fga=18, ftm=5, fta=6, pts=29)  # Jokic (opponent), C


def snapshot(c, fake: FakeFlaim | None = None, **kwargs) -> flaim_league.Snapshot:
    return flaim_league.build_snapshot(reader(fake), CFG, mapper(), DAY, c, now=NOW, **kwargs)


# ------------------------------------------------------------------ the snapshot


def test_snapshot_has_the_yahoo_path_semantics(conn):
    week_lines(conn)
    snap = snapshot(conn)
    lg = snap.league
    assert isinstance(lg, LeagueState) and lg.source == "yahoo"
    assert (lg.league_key, lg.name, lg.season, lg.my_team_key) == (LK, "Hardwood Classic", 2026, ME)
    assert len(lg.teams) == 12 and lg.opponent_key == OPP
    assert lg.categories == NINE_CAT and lg.max_weekly_adds == 4
    assert [(s.position, s.count, s.is_starting) for s in lg.slots][:2] == [("PG", 1, True), ("SG", 1, True)]
    assert lg.roster_size() == 13
    booker = next(p for p in lg.my_team.players if p.name == "Devin Booker")
    assert (booker.player_id, booker.nba_team, booker.selected, booker.yahoo_key) == (
        "e6002",
        "PHX",
        "SG",
        "466.p.6002",
    )
    assert booker.eligible == ("PG", "SG", "G", "UTIL")
    assert lg.team(f"{LK}.t.5").players[0].player_id.startswith("yahoo:")  # unknown to ESPN, kept by Yahoo id
    assert lg.waiver_priority[ME] == 11 and len(lg.standings) == 12
    assert lg.on_waivers == {"yahoo:6101": "2026-11-19"}
    assert lg.ownership["yahoo:6102"] == [55.0, None]
    assert lg.week == 5 and len(lg.matchups) == 6 and lg.fetched_at == "2026-11-18T19:00:00+00:00"
    assert sum(1 for m in snap.matches if not m.espn_id) > 0  # the other teams' players: surfaced by check


def test_week_window_falls_back_to_the_calendar_week(conn):
    snap = snapshot(conn)
    assert (snap.league.week_start, snap.league.week_end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert snap.week_source == "calendar"
    assert any("dates not sent by Flaim" in w for w in snap.warnings)


def test_adds_used_counted_from_transactions_since_the_week_began(conn):
    lg = snapshot(conn).league
    assert lg.adds_used[ME] == 1 and lg.adds_used[OPP] == 1  # the trade and the plain drop don't count
    assert lg.adds_used[f"{LK}.t.3"] == 0 and len(lg.adds_used) == 12


def test_banked_makes_attempts_rebuilt_from_lineups_and_box_scores(conn):
    week_lines(conn)
    snap = snapshot(conn)
    me = snap.league.banked[ME]
    assert me["pts"] == 301 and me["reb"] == 128 and me["tov"] == 31  # Yahoo's own counting stats
    assert me["fga"] == 47  # Haliburton 15 + Booker 20 + J. Williams 12 (bench and IL days excluded)
    assert me["fgm"] == pytest.approx(0.482 * 47, abs=0.01)  # Yahoo's FG% on the rebuilt attempts
    assert me["fta"] == 13 and me["ftm"] == pytest.approx(0.791 * 13, abs=0.01)
    opp = snap.league.banked[OPP]
    assert opp["fga"] == 18 and opp["fgm"] == pytest.approx(0.455 * 18, abs=0.01)
    assert snap.banked_source == {ME: "yahoo + box scores", OPP: "yahoo + box scores"}
    assert any("FT%: Yahoo 0.791 vs box scores 0.846" in w for w in snap.warnings)
    assert any("PTS: Yahoo 301 vs box scores 63" in w for w in snap.warnings)
    assert f"{LK}.t.3" not in snap.league.banked or "fga" not in snap.league.banked[f"{LK}.t.3"]


def test_past_lineups_are_fetched_once_then_kept(conn):
    fake = FakeFlaim()
    snapshot(conn, fake)
    dated = [a for name, a in fake.calls if name == "get_roster" and a.get("as_of_date")]
    assert {(a["team_id"], a["as_of_date"]) for a in dated} == {
        (t, d) for t in (ME, OPP) for d in ("2026-11-16", "2026-11-17")
    }
    again = FakeFlaim()
    snapshot(conn, again)
    assert not [a for name, a in again.calls if name == "get_roster" and a.get("as_of_date")]
    stored = conn.execute("SELECT selected_position FROM yahoo_daily_lineups WHERE espn_id = 'e6010' "
                          "AND et_date = '2026-11-16'").fetchone()[0]  # fmt: skip
    assert stored == "BN"


def test_snapshot_without_a_database_skips_the_rebuild(conn):
    snap = flaim_league.build_snapshot(reader(), CFG, mapper(), DAY, None, now=NOW)
    assert "fga" not in snap.league.banked[ME] and snap.league.banked[ME]["pts"] == 301
    assert any("no local database" in w for w in snap.warnings)


def test_flaim_week_dates_win(conn):
    payload = fixture("get_matchups")
    payload["data"].update(weekStart="2026-11-16", weekEnd="2026-11-29")
    snap = snapshot(conn, FakeFlaim({"get_matchups": payload}))
    assert (snap.league.week_start, snap.league.week_end, snap.week_source) == (
        date(2026, 11, 16),
        date(2026, 11, 29),
        "flaim",
    )


def test_wrong_team_fails_clearly(conn):
    cfg = LeagueConfig("466", "2026", LK, "Hardwood Classic", f"{LK}.t.99", "Ghost")
    with pytest.raises(Exception, match="isn't in"):
        flaim_league.build_snapshot(reader(), cfg, mapper(), DAY, conn, now=NOW)


# ------------------------------------------------------------------ week window (pure)


def _league() -> LeagueState:
    snap = flaim_league.build_snapshot(reader(), CFG, mapper(), DAY, None, now=NOW)
    lg = snap.league
    lg.week_start = lg.week_end = None
    return lg


def test_calendar_week_starts_on_opening_night():
    assert flaim_league.calendar_week(date(2026, 10, 22), date(2026, 10, 20)) == (
        date(2026, 10, 20),
        date(2026, 10, 25),
    )
    assert flaim_league.calendar_week(DAY, date(2026, 10, 20)) == (date(2026, 11, 16), date(2026, 11, 22))


def test_scheduled_games_cap_at_the_starting_slots():
    schedule = {date(2026, 11, 18): {"IND", "PHX"}, date(2026, 11, 19): {"IND"}}
    days = [date(2026, 11, 18), date(2026, 11, 19)]
    assert flaim_league.scheduled_games(["IND", "IND", "PHX"], schedule, days, 10) == 5
    assert flaim_league.scheduled_games(["IND", "IND", "PHX"], schedule, days, 2) == 4


def test_two_week_matchup_detected_from_yahoos_games_left():
    assert flaim_league.looks_two_week(60, 25)
    assert not flaim_league.looks_two_week(33, 25)
    assert not flaim_league.looks_two_week(None, 25)


def test_fill_week_prefers_a_remembered_window_then_stretches_a_merged_week():
    lg, warnings = _league(), []
    remembered = {5: (date(2026, 11, 9), date(2026, 11, 22))}
    assert flaim_league.fill_week(lg, {}, DAY, remembered, None, None, warnings) == "remembered"
    assert (lg.week_start, lg.week_end) == remembered[5]
    lg, warnings = _league(), []
    schedule = {DAY + timedelta(days=i): {"IND", "PHX", "NY", "MEM"} for i in range(12)}
    source = flaim_league.fill_week(lg, {}, DAY, {}, 70, schedule, warnings)
    assert source == "calendar (two-week)" and lg.week_end == date(2026, 11, 29)
    assert any("two-week matchup" in w for w in warnings)


def test_remembered_window_is_stretched_when_yahoo_keeps_playing_the_week():
    lg, warnings = _league(), []
    remembered = {5: (date(2026, 11, 9), date(2026, 11, 15))}  # recorded last week, before the merge showed
    source = flaim_league.fill_week(lg, {}, DAY, remembered, 20, None, warnings)
    assert source == "remembered (two-week)" and (lg.week_start, lg.week_end) == (date(2026, 11, 9), date(2026, 11, 22))
    lg, warnings = _league(), []
    lag = flaim_league.fill_week(lg, {}, DAY, remembered, 0, None, warnings)  # week over, not rolled over yet
    assert lag == "remembered" and lg.week_end == date(2026, 11, 15)


def test_no_schedule_means_no_two_week_guess():
    lg, warnings = _league(), []
    empty = {DAY + timedelta(days=i): set() for i in range(12)}
    assert flaim_league.fill_week(lg, {}, DAY, {}, 70, empty, warnings) == "calendar"
    assert lg.week_end == date(2026, 11, 22) and any("fbm data sync" in w for w in warnings)


def test_a_finished_week_keeps_its_own_dates_so_fbm_rolls_over_like_on_yahoo():
    lg, warnings = _league(), []
    monday = date(2026, 11, 23)  # Yahoo still reports week 5 (final) until it rolls over
    source = flaim_league.fill_week(lg, {}, monday, {}, None, {}, warnings, finished=True)
    assert source == "calendar" and (lg.week_start, lg.week_end) == (date(2026, 11, 16), date(2026, 11, 22))


def test_week_windows_are_kept_per_league(tmp_path):
    path = tmp_path / "weeks.json"
    flaim_league.remember_week(path, LK, 5, date(2026, 11, 16), date(2026, 11, 22))
    flaim_league.remember_week(path, "466.l.1", 5, date(2025, 11, 17), date(2025, 11, 23))
    assert flaim_league.reads.load_week_windows(LK, path) == {5: (date(2026, 11, 16), date(2026, 11, 22))}
    assert flaim_league.reads.load_week_windows("466.l.9", path) == {}
    path.write_text("not json")
    assert flaim_league.reads.load_week_windows(LK, path) == {}
    flaim_league.remember_week(path, LK, 6, date(2026, 11, 23), date(2026, 11, 29))
    assert flaim_league.reads.load_week_windows(LK, path) == {6: (date(2026, 11, 23), date(2026, 11, 29))}


def test_adds_used_ignores_trades_failed_and_old_moves():
    since = flaim_league.week_start_utc(date(2026, 11, 16))
    assert since == datetime(2026, 11, 16, 5, 0, tzinfo=UTC)

    def tx(kind, status, when, team):
        leg = TxPlayer("466.p.1", "1", "X", "BOS", "add", "freeagents", "", "team", team)
        return Transaction(f"k{when}", kind, status, when, (leg,))

    txs = [
        tx("add/drop", "successful", since + timedelta(hours=1), ME),
        tx("add", "failed", since + timedelta(hours=2), ME),
        tx("trade", "successful", since + timedelta(hours=3), ME),
        tx("add", "successful", since - timedelta(hours=1), ME),
    ]
    assert flaim_league.adds_used(txs, since, [ME, OPP]) == {ME: 1, OPP: 0}


# ------------------------------------------------------------------ banked rebuild (pure)


def box(day: str, **stats) -> dict:
    return {"et_date": day, **dict.fromkeys(banked.TOTAL_COLS, 0), **stats}


def test_reconstruct_counts_only_active_slot_days():
    lineups = {"2026-11-16": {"a": "PG", "b": "BN"}, "2026-11-17": {"a": "IL", "b": "UTIL"}}
    rows = {
        "a": [box("2026-11-16", pts=1, fga=1), box("2026-11-17", pts=9)],  # IL on the 17th
        "b": [box("2026-11-16", pts=50), box("2026-11-17", pts=2, fga=2)],  # bench on the 16th
        "c": [box("2026-11-16", pts=99)],  # never on this roster
    }
    out = banked.reconstruct(lineups, rows)
    assert out["pts"] == 3 and out["fga"] == 3 and out["games"] == 2


def test_merge_keeps_yahoos_numbers_and_fills_the_gaps():
    rebuilt = {**dict.fromkeys(banked.TOTAL_COLS, 0.0), "fgm": 50.0, "fga": 100.0, "ftm": 20.0, "fta": 25.0, "pts": 300}
    rebuilt.update(reb=120, ast=60, stl=10, blk=8, tov=25, tpm=20)
    reported = {"5": ".500", "8": ".800", "12": "300", "15": "120", "16": "60", "17": "10", "18": "8", "19": "25"}
    reported["10"] = "20"
    out = banked.merge(reported, rebuilt, "Me")
    assert out.stats["fga"] == 100 and out.stats["fgm"] == 50 and out.stats["ftm"] == 20
    assert out.source == "yahoo + box scores" and not out.warnings
    exact = banked.merge({**reported, "9004003": "48/99", "9007006": "19/24"}, rebuilt, "Me")
    assert (exact.stats["fgm"], exact.stats["fga"], exact.source) == (48, 99, "yahoo")
    silent = banked.merge({}, rebuilt, "Me")
    assert silent.stats["pts"] == 300 and silent.stats["fgm"] == 50 and silent.source == "box scores"
    alone = banked.merge(reported, None, "Me")
    assert "fga" not in alone.stats and alone.source == "yahoo (no makes/attempts)"


def test_merge_without_attempts_keeps_zero_and_does_not_divide():
    rebuilt = dict.fromkeys(banked.TOTAL_COLS, 0.0)
    out = banked.merge({"5": ".480", "12": "0"}, rebuilt, "Me")
    assert out.stats["fga"] == 0 and out.stats["fgm"] == 0


# ------------------------------------------------------------------ saving, routing, history


def test_refresh_routes_through_flaim_and_saves_the_snapshot(tmp_path, monkeypatch, conn):
    from fbm.league import yahoo_league

    week_lines(conn)
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    monkeypatch.setattr(yahoo_league, "SNAPSHOT_PATH", tmp_path / "yahoo_league.json")
    monkeypatch.setattr(yahoo_league, "SETTINGS_SUMMARY_PATH", tmp_path / "summary.json")
    monkeypatch.setattr(flaim_league, "WEEKS_PATH", tmp_path / "weeks.json")
    monkeypatch.setattr(flaim_league, "WARNINGS_PATH", tmp_path / "warnings.json")
    monkeypatch.setattr("fbm.store.db.connect", lambda *a, **k: conn)
    monkeypatch.setattr("fbm.config.load_league_config", lambda *a, **k: CFG)
    monkeypatch.setattr("fbm.yahoo.ids.espn_players_from_db", lambda c: mapper().players)
    monkeypatch.setattr("fbm.yahoo.ids.load_overrides", lambda *a: {})
    monkeypatch.setattr(FlaimLeague, "from_config", classmethod(lambda cls, client=None: reader()))
    monkeypatch.setattr(
        "fbm.yahoo.client.YahooClient", lambda *a, **k: pytest.fail("the Flaim route must not touch Yahoo")
    )
    league, unmatched = yahoo_league.refresh(DAY)
    assert league.opponent_key == OPP and unmatched
    saved = json.loads((tmp_path / "yahoo_league.json").read_text())
    assert saved["league_key"] == LK and saved["banked"][ME]["fga"] == 47
    assert json.loads((tmp_path / "weeks.json").read_text()) == {LK: {"5": ["2026-11-16", "2026-11-22"]}}
    assert flaim_league.reads.load_week_windows(LK, tmp_path / "weeks.json") == {
        5: (date(2026, 11, 16), date(2026, 11, 22))
    }
    warn = json.loads((tmp_path / "warnings.json").read_text())
    assert warn["week_source"] == "calendar" and warn["banked_source"][ME] == "yahoo + box scores"
    assert json.loads((tmp_path / "summary.json").read_text())["current_week"] == "5"
    mapped = conn.execute("SELECT espn_id FROM yahoo_players WHERE yahoo_id = '6002'").fetchone()[0]
    assert mapped == "e6002"


def test_transaction_history_syncs_by_date_range(conn):
    fake = FakeFlaim()
    r = reader(fake)
    first = flaim_league.sync_transactions(conn, r, today=DAY)
    assert first["new"] == 4 and first["total_stored"] == 4 and first["window"][0] == "2025-11-18"
    args = fake.calls[-1][1]
    assert (args["start_date"], args["end_date"]) == ("2025-11-18", "2026-11-18") and "count" not in args
    again = flaim_league.sync_transactions(conn, r, today=DAY)
    assert again["new"] == 0 and again["window"][0] == "2026-11-16"  # the day before the newest stored move


def test_tx_history_sync_dispatches_to_flaim(conn):
    from fbm.league import tx_history

    out = tx_history.sync(conn, reader(), LK)
    assert out["fetched"] == 4 and {t.key for t in tx_history.load(conn)} >= {f"{LK}.tr.140", f"{LK}.tr.138"}


def test_snapshot_end_to_end_over_mcp(conn):
    """The real client and reader together: JSON-RPC over a mock streamable-HTTP server answering from fixtures."""
    import httpx

    from fbm.flaim.client import FlaimClient
    from tests.test_flaim_client import FakeServer, FakeTokens, rpc_result, tool_result

    fixtures = FakeFlaim()

    def answer(request, body):
        params = body["params"]
        payload = fixtures.call_tool(params["name"], params["arguments"])
        return httpx.Response(200, json=rpc_result(body, tool_result(payload)))

    server = FakeServer(*[answer] * 40)
    http = httpx.Client(transport=httpx.MockTransport(server))
    client = FlaimClient("https://api.flaim.app/mcp", FakeTokens(), http, min_interval=0)
    snap = flaim_league.build_snapshot(FlaimLeague(client, LK, 2026, ME, {}), CFG, mapper(), DAY, conn, now=NOW)
    assert snap.league.opponent_key == OPP and len(snap.league.teams) == 12 and server.inits == 1
    assert {b["params"]["name"] for _, b in server.calls()} >= {"get_league_info", "get_roster", "get_matchups"}
