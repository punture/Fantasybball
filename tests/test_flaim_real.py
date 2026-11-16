"""Flaim's REAL responses (tests/fixtures/flaim/real/: one live Yahoo NBA 9-cat league on 2026-11-16, Monday of
week 5; team/owner names and the league id anonymized, shapes and values verbatim) read through the production path:
FlaimClient (JSON-RPC over a mock streamable-HTTP server) -> FlaimLeague -> the snapshot. Offline.

What these payloads settle: category rows carry Yahoo's display-only FGM/A (9004003) and FTM/A (9007006) next to
FG%/FT%; at the start of a week values are "" and "/"; no week dates anywhere; standings mix strings and ints;
historical daily rosters have no NBA team and no eligibility; other teams' rows omit isEditable; transactions name
teams by full team key; the session's cached team name can be stale (league info is current)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from fbm.analytics.categories import NINE_CAT
from fbm.config import LeagueConfig
from fbm.flaim import check as flaim_check
from fbm.flaim import league as flaim_league
from fbm.flaim import reads
from fbm.flaim.client import FlaimClient
from fbm.flaim.reads import FlaimLeague
from fbm.store import db
from fbm.yahoo import lineups
from fbm.yahoo.ids import EspnPlayer, IdMapper, espn_team
from fbm.yahoo.league_reads import banked_stats
from tests.test_flaim_client import FakeTokens, rpc_result, tool_result

REAL = Path(__file__).parent / "fixtures" / "flaim" / "real"
LK = "999.l.12345"
ME, OPP, T6 = f"{LK}.t.8", f"{LK}.t.5", f"{LK}.t.6"
MONDAY = date(2026, 11, 16)  # week 5 began today (ET); the fixtures were read at 13:21 UTC
NOW = datetime(2026, 11, 16, 13, 21, tzinfo=UTC)
CFG = LeagueConfig("999", "2026", LK, "Test League", ME, "My Old Team Name")  # the session's stale team name
URL = "https://api.flaim.app/mcp"


def real(name: str) -> dict:
    return json.loads((REAL / f"{name}.json").read_text(encoding="utf-8"))


class RealServer:
    """Flaim's MCP endpoint answering tools/call from the real fixtures; records every tool call."""

    def __init__(self, overrides: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.overrides = overrides or {}

    def payload(self, name: str, args: dict) -> dict:
        if name in self.overrides:
            return copy.deepcopy(self.overrides[name])
        if name == "get_roster":
            team, day = args["team_id"], args.get("as_of_date")
            if team == ME:
                return real("get_roster_t8_2026-11-15" if day else "get_roster_t8")
            if team == OPP:
                return real("get_roster_t5")
            return {"success": True, "data": {"teamKey": team, "snapshot": {"type": "current"}, "players": []}}
        if name == "get_matchups":
            return real("get_matchups_week4" if args.get("week") == 4 else "get_matchups_week5")
        if name == "get_transactions" and args.get("type") in ("waiver", "pending_trade"):
            return {"success": True, "data": {"transactions": [], "count": 0}}
        return real(name)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "flaim"}}
            return httpx.Response(200, json=rpc_result(body, result), headers={"mcp-session-id": "s1"})
        if method == "notifications/initialized":
            return httpx.Response(202)
        name, args = body["params"]["name"], body["params"]["arguments"]
        self.calls.append((name, dict(args)))
        return httpx.Response(200, json=rpc_result(body, tool_result(self.payload(name, args))))

    def dated_rosters(self) -> list[dict]:
        return [a for n, a in self.calls if n == "get_roster" and a.get("as_of_date")]


def real_reader(server: RealServer | None = None) -> tuple[FlaimLeague, RealServer]:
    server = server or RealServer()
    http = httpx.Client(transport=httpx.MockTransport(server))
    client = FlaimClient(URL, FakeTokens(), http, min_interval=0)
    return FlaimLeague(client, LK, 2026, ME, week_windows={}), server


def espn_mapper() -> IdMapper:
    people = [
        EspnPlayer(f"e{p['playerId']}", p["name"], espn_team(p["team"]))
        for name in ("get_roster_t8", "get_roster_t5", "get_free_agents")
        for p in real(name)["data"]["players" if "roster" in name else "freeAgents"]
    ]
    return IdMapper(people, {})


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("fbm.timeutil.nba_today", lambda now=None: MONDAY)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# ------------------------------------------------------------------ the snapshot, end to end


def test_real_snapshot_through_refresh(tmp_path, monkeypatch, conn):
    """`fbm snapshot` on `[yahoo] route = "flaim"`: yahoo_league.refresh -> read_snapshot -> FlaimClient over HTTP."""
    from fbm.league import yahoo_league

    server = RealServer()
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    monkeypatch.setattr(yahoo_league, "SNAPSHOT_PATH", tmp_path / "yahoo_league.json")
    monkeypatch.setattr(yahoo_league, "SETTINGS_SUMMARY_PATH", tmp_path / "summary.json")
    monkeypatch.setattr(flaim_league, "WEEKS_PATH", tmp_path / "weeks.json")
    monkeypatch.setattr(flaim_league, "WARNINGS_PATH", tmp_path / "warnings.json")
    monkeypatch.setattr("fbm.store.db.connect", lambda *a, **k: conn)
    monkeypatch.setattr("fbm.config.load_league_config", lambda *a, **k: CFG)
    monkeypatch.setattr("fbm.yahoo.ids.espn_players_from_db", lambda c: espn_mapper().players)
    monkeypatch.setattr("fbm.yahoo.ids.load_overrides", lambda *a: {})
    monkeypatch.setattr(FlaimLeague, "from_config", classmethod(lambda cls, client=None: real_reader(server)[0]))
    monkeypatch.setattr(
        "fbm.yahoo.client.YahooClient", lambda *a, **k: pytest.fail("the Flaim route must not touch Yahoo")
    )
    lg, unmatched = yahoo_league.refresh(MONDAY)

    assert (lg.league_key, lg.name, lg.season, lg.source) == (LK, "Test League", 2026, "yahoo")
    assert lg.my_team_key == ME and lg.my_team.name == "My Team"  # league info's name, not the session's
    assert len(lg.teams) == 10 and lg.opponent_key == OPP
    assert lg.categories == NINE_CAT and lg.max_weekly_adds == 4
    slots = [(s.position, s.count, s.is_starting) for s in lg.slots]
    assert slots == [
        ("PG", 1, True),
        ("SG", 1, True),
        ("G", 1, True),
        ("SF", 1, True),
        ("PF", 1, True),
        ("F", 1, True),
        ("C", 2, True),
        ("UTIL", 2, True),
        ("BN", 3, False),
        ("IL", 5, False),
    ]
    assert lg.week == 5 and (lg.week_start, lg.week_end) == (date(2026, 11, 16), date(2026, 11, 22))
    assert lg.adds_used[ME] == 1 and sum(lg.adds_used.values()) == 1  # tr.165 this morning; Sunday's are week 4
    assert lg.waiver_priority[ME] == 7 and lg.waiver_priority[f"{LK}.t.10"] == 1
    rows = {r["team_key"]: r for r in lg.standings}
    assert (rows[OPP]["rank"], rows[OPP]["wins"], rows[OPP]["losses"], rows[OPP]["ties"]) == (1, 22, 13, 1)
    assert (rows[ME]["rank"], rows[ME]["ties"], rows[ME]["pct"]) == (4, 0, 0.556)

    me = {p.name: p for p in lg.my_team.players}
    assert len(me) == 15 and me["Nikola Jokić"].selected == "C" and me["Yaxel Lendeborg"].selected == "UTIL"
    assert me["Isaiah Hartenstein"].selected == "BN" and me["Stephen Curry"].selected == "IL"
    assert me["Stephen Curry"].status == "O" and "IL" in me["Stephen Curry"].eligible
    assert me["Deni Avdija"].eligible == ("SG", "G", "SF", "PF", "F", "UTIL")
    assert me["Jalen Suggs"].player_id == "e6516" and me["Jalen Suggs"].yahoo_key == "478.p.6516"
    assert len(lg.team(OPP).players) == 12

    # Start of the week: every value empty, FGM/A "/" -> nothing banked; no past day to read yet
    assert lg.banked[ME]["fga"] == 0 and set(lg.banked[ME].values()) == {0.0}
    assert lg.banked[f"{LK}.t.1"] == {"fgm": 0.0, "fga": 0.0, "ftm": 0.0, "fta": 0.0}  # "/" parsed as Yahoo does
    assert not server.dated_rosters()
    assert lg.matchups[0]["status"] == "midevent" and len(lg.matchups) == 5
    mine = next(m for m in lg.matchups if any(t["team_key"] == ME for t in m["teams"]))
    assert set(mine["stat_winners"]) == {c.yahoo_stat_id for c in NINE_CAT}
    assert all(w is None for w in mine["stat_winners"].values())  # all tied at 0

    assert lg.on_waivers == {"e4622": "2026-11-17", "e10340": "2026-11-17", "e10312": "2026-11-18"}
    assert lg.ownership["e6513"] == [67.0, None] and len(lg.ownership) == 12
    assert not [m for m in unmatched if m.yahoo_id in ("6516", "5352")]

    weeks = json.loads((tmp_path / "weeks.json").read_text())
    assert weeks == {LK: {"5": ["2026-11-16", "2026-11-22"]}}
    warn = json.loads((tmp_path / "warnings.json").read_text())
    assert warn["week_source"] == "calendar" and any("dates not sent by Flaim" in w for w in warn["warnings"])


def test_real_midweek_banked_stats_come_straight_from_flaim(conn, monkeypatch):
    """Mid-week, Flaim's FGM/A and FTM/A rows are Yahoo's own makes/attempts: no daily-lineup reads, no rebuild."""
    friday = date(2026, 11, 13)
    monkeypatch.setattr("fbm.timeutil.nba_today", lambda now=None: friday)
    info = real("get_league_info")
    info["data"]["currentWeek"] = 4
    board = real("get_matchups_week4")
    for m in board["data"]["matchups"]:
        m["status"] = "in_progress"
    server = RealServer({"get_league_info": info, "get_matchups": board})
    reader, _ = real_reader(server)
    snap = flaim_league.build_snapshot(reader, CFG, espn_mapper(), friday, conn, now=NOW)
    lg = snap.league
    assert (
        lg.week == 4
        and lg.opponent_key == T6
        and (lg.week_start, lg.week_end)
        == (
            date(2026, 11, 9),
            date(2026, 11, 15),
        )
    )
    assert lg.banked[ME] == {
        "fgm": 246.0,
        "fga": 499.0,
        "ftm": 122.0,
        "fta": 140.0,
        "tpm": 101.0,
        "pts": 715.0,
        "reb": 250.0,
        "ast": 153.0,
        "stl": 41.0,
        "blk": 24.0,
        "tov": 84.0,
    }
    assert lg.banked[T6]["fga"] == 603.0 and lg.banked[f"{LK}.t.1"]["fta"] == 184.0  # every team, not just mine
    assert snap.banked_source == {ME: "yahoo", T6: "yahoo"}
    assert not server.dated_rosters()  # nothing to rebuild


def test_real_empty_makes_attempts_fall_back_to_the_rebuild_mid_week(conn):
    """If Flaim ever sends "/" once games were played, the box-score rebuild fills makes/attempts."""
    from tests.test_flaim_league import line

    board = real("get_matchups_week5")
    mine = next(m for m in board["data"]["matchups"] if m["away"]["teamId"] == "8")
    for row in mine["away"]["categories"]:
        row["value"] = {"5": ".500", "12": "20"}.get(row["statId"], row["value"])
    tuesday = date(2026, 11, 17)
    conn.execute(
        "INSERT INTO games(event_id, season, season_type, start_utc, et_date, home, away, state, completed, neutral, "
        "time_valid, box_synced, updated_at) VALUES ('g1',2027,2,'2026-11-16T23:00:00+00:00','2026-11-16','NY','TOR',"
        "'post',1,0,1,1,'x')"
    )
    line(conn, "2026-11-16", "e6417", fgm=4, fga=8, ftm=2, fta=2, pts=20)  # Quickley: PG in the day-before lineup
    server = RealServer({"get_matchups": board})
    reader, _ = real_reader(server)
    snap = flaim_league.build_snapshot(reader, CFG, espn_mapper(), tuesday, conn, now=NOW)
    assert [a["as_of_date"] for a in server.dated_rosters() if a["team_id"] == ME] == ["2026-11-16"]
    assert snap.banked_source[ME] == "yahoo + box scores"
    assert snap.league.banked[ME]["fga"] == 8 and snap.league.banked[ME]["fgm"] == 4  # .500 of the rebuilt 8


# ------------------------------------------------------------------ the feeds one by one


def test_real_rosters_slots_eligibility_and_editability():
    reader, server = real_reader()
    mine = reader.roster(ME, MONDAY)
    assert mine.is_editable and len(mine.players) == 15
    opp = reader.roster(OPP, MONDAY)  # other teams' rows omit isEditable: not my lineup to set
    assert not opp.is_editable and len(opp.players) == 12
    past = reader.roster(ME, date(2026, 11, 15), current=False)
    assert not past.is_editable and server.calls[-1][1]["as_of_date"] == "2026-11-15"
    rows = {p.name: p for p in past.players}
    assert rows["Sam Hauser"].selected_position == "UTIL" and rows["Sam Hauser"].nba_team == ""
    assert rows["Immanuel Quickley"].eligible_positions == ("PG", "SG", "G")  # derived: no Yahoo list for past days
    assert rows["Chet Holmgren"].selected_position == "IL"
    assert not reader.warnings  # a dated snapshot without eligibility is expected, not a warning


def test_real_week4_scoreboard_and_retro_finals(conn):
    reader, _ = real_reader()
    board = reader.scoreboard(LK, 4)
    mine = next(m for m in board if m.team(ME))
    assert (mine.week, mine.status, mine.opponent_of(ME).team_key) == (4, "postevent", T6)
    assert mine.team(ME).stats["9004003"] == "246/499" and mine.team(ME).points == 5.0
    assert banked_stats(mine.team(ME).stats)["fga"] == 499.0
    assert mine.stat_winners == {
        "5": ME,
        "8": ME,
        "10": ME,
        "12": T6,
        "15": ME,
        "16": T6,
        "17": T6,
        "18": T6,
        "19": ME,
    }
    other = next(m for m in board if m.team(OPP))
    assert other.stat_winners["17"] is None  # steals tied 38-38
    assert all(m.week_start is None for m in board)  # Flaim sends no week dates

    dated = FlaimLeague(reader.client, LK, 2026, ME, week_windows={4: (date(2026, 11, 9), date(2026, 11, 15))})
    assert lineups.store_matchups(conn, dated.scoreboard(LK, 4)) == 6
    final = lineups.final_for(conn, date(2026, 11, 12), ME)
    assert final["final"] and final["opponent_key"] == T6
    assert (final["me"]["fga"], final["opp"]["fga"]) == (499.0, 603.0)  # graders get real makes/attempts
    assert final["stat_winners"]["fg_pct"] == 1.0 and final["stat_winners"]["pts"] == 0.0


def test_real_settings_summary():
    from fbm.yahoo.settings import summarize

    reader, _ = real_reader()
    s = summarize(reader.settings_raw(LK))
    assert [c["stat_id"] for c in s["scoring_categories"]] == ["5", "8", "10", "12", "15", "16", "17", "18", "19"]
    assert [c["stat_id"] for c in s["display_only_stats"]] == ["9004003", "9007006"]
    assert s["settings"]["max_weekly_adds"] == "4" and s["settings"]["waiver_type"] == "R"
    assert s["num_teams"] == "10" and s["current_week"] == "5" and s["end_week"] == "22"
    assert {r["position"]: (r["count"], r["is_starting"]) for r in s["roster_slots"]}["IL"] == (5, False)
    assert not reader.warnings


def test_real_transactions_are_attributed_by_team_key():
    reader, _ = real_reader()
    txs = reader.transactions(LK, 100)
    assert [t.key for t in txs][:2] == [f"{LK}.tr.165", f"{LK}.tr.164"]
    first = txs[0]
    assert first.type == "add/drop" and first.status == "successful"
    assert first.timestamp == datetime(2026, 11, 16, 13, 14, 29, tzinfo=UTC)
    add, drop = first.players
    assert (add.action, add.player_id, add.source_type, add.destination_team_key) == ("add", "6038", "freeagents", ME)
    assert (drop.action, drop.player_id, drop.source_team_key, drop.destination_type) == ("drop", "6619", ME, "waivers")
    assert [(t.type, t.players[0].source_team_key) for t in txs if t.type == "drop"] == [
        ("drop", f"{LK}.t.1"),
        ("drop", ME),
    ]
    week5 = flaim_league.week_start_utc(MONDAY)
    week4 = flaim_league.week_start_utc(date(2026, 11, 9))
    assert flaim_league.adds_used(txs, week5, [ME, f"{LK}.t.4"]) == {ME: 1, f"{LK}.t.4": 0}
    assert flaim_league.adds_used(txs, week4, [ME, f"{LK}.t.4"]) == {ME: 3, f"{LK}.t.4": 1}


def test_real_free_agents():
    reader, _ = real_reader()
    pool = reader.available(LK)
    assert len(pool) == 12
    holiday = next(p for p in pool if p.name == "Jrue Holiday")
    assert (holiday.ownership_type, holiday.waiver_date, holiday.percent_owned) == (
        "waivers",
        date(2026, 11, 17),
        57.0,
    )
    assert {p.status for p in pool} == {"", "Q", "O", "NA"}
    assert [p.name for p in reader.players(LK, "W")] == ["Jrue Holiday", "Jaylen Wells", "Pelle Larsson"]


def test_real_session_team_name_is_stale_league_info_wins():
    reader, _ = real_reader()
    ref = reads.session_leagues(real("get_user_session"))[0]
    assert (ref.league_key, ref.team_key, ref.season_year, ref.team_name) == (LK, ME, 2026, "My Old Team Name")
    assert reader.my_teams() == [{"team_key": ME, "name": "My Team"}]
    row = reader.leagues()[0]
    assert (row["name"], row["num_teams"], row["current_week"], row["game_key"]) == ("Test League", "10", "5", "999")


def test_real_league_select_writes_the_current_team_name(monkeypatch):
    from fbm import cli_flaim
    from fbm.cli import app

    saved: list[LeagueConfig] = []
    monkeypatch.setattr(cli_flaim, "_reader", lambda **kw: real_reader()[0])
    monkeypatch.setattr(cli_flaim, "save_league_config", saved.append)
    result = CliRunner().invoke(app, ["flaim", "leagues", "--select", "--league-id", "12345"])
    assert result.exit_code == 0, result.output
    assert saved == [LeagueConfig("999", "2026", LK, "Test League", ME, "My Team")]


def test_real_check_reports_no_failures(conn):
    reader, _ = real_reader()
    report = flaim_check.run(reader, CFG, MONDAY, conn)
    feeds = {f.name: f for f in report.feeds}
    assert not report.failed, [(f.name, f.detail) for f in report.feeds]
    assert feeds["League info"].status == "OK" and "9/9 categories mapped" in feeds["League info"].detail
    assert feeds["Matchups (this week)"].status == "OK"
    assert "9/9 category rows, 0 with values" in feeds["Matchups (this week)"].detail
    assert feeds["Standings"].detail.startswith("10 teams, 10 with waiver priority; you: rank 4, 20-16-0")
    assert feeds["Transactions"].status == "OK"
    assert feeds["Your roster (today)"].detail.startswith("15 players (10 in active slots")


def test_real_read_tools_on_the_flaim_route(monkeypatch, conn):
    """The services FBM's tools call (scoreboard, standings, transactions, offers, history sync) on real payloads."""
    from fbm.gm import services as svc

    reader, _ = real_reader()
    league = flaim_league.build_snapshot(reader, CFG, espn_mapper(), MONDAY, None, now=NOW).league
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_yahoo_client", lambda: real_reader()[0])
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr("fbm.yahoo.ids.espn_players_from_db", lambda c: espn_mapper().players)
    monkeypatch.setattr("fbm.yahoo.ids.load_overrides", lambda: {})

    board = svc.scoreboard_tool()
    assert board["week"] == 5 and len(board["matchups"]) == 5
    assert svc.standings_tool()["standings"][0]["team_key"] == OPP
    mine = svc.transactions_tool(25, "My Team")
    assert mine["count"] == 4  # tr.165, 162, 159, 158: the league-wide 6 minus team 1's drop and team 4's add
    offers = svc.pending_offers_tool()
    assert offers["incoming_trades"] == [] and offers["outgoing_trades"] == []
    synced = svc.sync_transactions_tool(write_memory=False)
    assert synced["new"] == 6 and synced["complete"]
