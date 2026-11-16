"""`[yahoo] route = "flaim"`: settings, the client factory, every Yahoo read routed through Flaim (or degrading with a
clear message), the health check, the services FBM's tools call, and the `fbm flaim` commands. Offline."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fbm import credentials
from fbm.analytics.categories import FG_PCT, PTS
from fbm.config import LeagueConfig, NotConfiguredError
from fbm.flaim import check as flaim_check
from fbm.flaim import oauth, route
from fbm.flaim.reads import FlaimLeague
from fbm.gm import health
from fbm.gm import services as svc
from fbm.league import yahoo_views
from fbm.settings import EXAMPLE_PATH, load_settings
from fbm.store import db
from fbm.yahoo import discovery, league_reads, lineups
from fbm.yahoo import roster as yahoo_roster
from fbm.yahoo import settings as yahoo_settings
from fbm.yahoo.client import YahooAPIError
from tests.test_flaim_client import signed_in, vault  # noqa: F401 - pytest fixture
from tests.test_flaim_league import CFG, NOW, mapper
from tests.test_flaim_reads import DAY, LK, ME, OPP, FakeFlaim, reader

runner = CliRunner()


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("fbm.timeutil.nba_today", lambda now=None: DAY)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# ------------------------------------------------------------------ settings and the route


def test_settings_parse_yahoo_and_flaim_blocks(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text('[yahoo]\nroute = "Flaim"\n[flaim]\nurl = "https://example.test/mcp"\n', encoding="utf-8")
    s = load_settings(path)
    assert (s.yahoo_route, s.flaim_url) == ("flaim", "https://example.test/mcp")
    assert load_settings(tmp_path / "missing.toml").yahoo_route == "auto"
    monkeypatch.setattr("fbm.settings.SETTINGS_PATH", path)
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "session")
    assert load_settings(path).yahoo_route == "session"  # per-process override of the owner's file


def test_route_comes_from_settings_or_env(monkeypatch):
    monkeypatch.delenv("FBM_YAHOO_ROUTE", raising=False)
    monkeypatch.setattr("fbm.flaim.route.load_settings", lambda: load_settings(Path("/nonexistent")))
    assert route.yahoo_route() == "auto" and not route.is_flaim()
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    assert route.yahoo_route() == "flaim" and route.is_flaim()
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "carrier-pigeon")
    with pytest.raises(NotConfiguredError, match="Unknown Yahoo route"):
        route.yahoo_route()
    assert not route.is_flaim()


def test_save_route_keeps_everything_else(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[owner]\ntimezone = "Asia/Manila"\n[league]\nsource = "yahoo"\n', encoding="utf-8")
    route.save_route("flaim", path, EXAMPLE_PATH)
    s = load_settings(path)
    assert (s.yahoo_route, s.owner_timezone, s.league_source) == ("flaim", "Asia/Manila", "yahoo")
    route.save_route("session", path, EXAMPLE_PATH)
    assert load_settings(path).yahoo_route == "session"
    with pytest.raises(NotConfiguredError):
        route.save_route("nope", path, EXAMPLE_PATH)


def test_league_client_per_route(monkeypatch):
    made = []
    monkeypatch.setattr(FlaimLeague, "from_config", classmethod(lambda cls, client=None: "flaim-reader"))
    monkeypatch.setattr("fbm.yahoo.client.YahooClient", lambda **kw: made.append(kw) or "yahoo-client")
    monkeypatch.setattr("fbm.yahoo.session.load", lambda: None)
    assert route.league_client("flaim") == "flaim-reader"
    assert route.league_client("auto") == "yahoo-client" and made[-1] == {}
    assert route.league_client("api") == "yahoo-client" and "tokens" in made[-1]
    with pytest.raises(YahooAPIError, match="fbm yahoo login"):
        route.league_client("session")


def test_services_use_the_route_factory(monkeypatch):
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    monkeypatch.setattr(FlaimLeague, "from_config", classmethod(lambda cls, client=None: "flaim-reader"))
    assert svc._yahoo_client() == "flaim-reader"


# ------------------------------------------------------------------ every Yahoo read, routed


def test_yahoo_fetch_functions_route_to_flaim():
    from fbm.league.yahoo_league import fetch_ownership

    fake = FakeFlaim()
    r = reader(fake)
    assert len(league_reads.fetch_teams(r, LK)) == 12
    assert league_reads.fetch_standings(r, LK)[0].team_key == OPP
    assert len(league_reads.fetch_scoreboard(r, LK, 5)) == 6 and fake.calls[-1][1]["week"] == 5
    assert [p.player_id for p in league_reads.fetch_players(r, LK, "W", 10)] == ["6101"]
    assert len(fetch_ownership(r, LK, 2)) == 2
    assert len(league_reads.fetch_transactions(r, LK, 10)) == 4
    assert league_reads.fetch_transactions(r, LK, 10, start=10) == []
    assert {t.type for t in league_reads.fetch_pending(r, LK, ME)} == {"waiver", "pending_trade"}
    assert len(yahoo_roster.get_roster(r, ME, DAY).players) == 14
    summary = yahoo_settings.summarize(yahoo_settings.fetch_settings(r, LK))
    assert len(summary["scoring_categories"]) == 9
    leagues, teams = discovery.list_nba_leagues(r), discovery.list_my_nba_teams(r)
    assert discovery.team_for_league(teams, leagues[0]["league_key"]) == {"team_key": ME, "name": "Glass Cleaners"}


def test_retro_sync_degrades_without_week_dates_and_works_with_them(conn):
    r = reader()

    def resolve(yahoo_id, name, team):
        return mapper().match(yahoo_id, name, team).espn_id

    out = lineups.sync_weeks(conn, r, LK, ME, [5], resolve)
    assert out == {"weeks": [], "lineup_rows": 0}  # no dates from Flaim: nothing stored, nothing broken
    dated = reader(week_windows={5: (date(2026, 11, 16), date(2026, 11, 22))})
    out = lineups.sync_weeks(conn, dated, LK, ME, [5], resolve)
    assert out["weeks"] == [{"week": 5, "final": False}] and out["lineup_rows"] > 0
    final = lineups.final_for(conn, date(2026, 11, 17), ME)
    assert final["opponent_key"] == OPP and final["me"]["pts"] == 301
    assert "fga" not in final["me"]  # Flaim sends percentages only: graders fall back for FG%/FT%
    assert lineups.started(conn, ME, date(2026, 11, 16))


def test_scoreboard_view_uses_yahoos_percentage_without_makes_attempts():
    stats = {"5": ".482", "12": "301"}
    assert yahoo_views.category_value(FG_PCT, stats) == 0.482
    assert yahoo_views.category_value(PTS, stats) == 301
    assert yahoo_views.category_value(FG_PCT, {"9004003": "0/0", "5": "-"}) is None  # Yahoo path unchanged
    assert yahoo_views.category_value(FG_PCT, {"12": "3"}) is None


@pytest.fixture
def live(monkeypatch, conn):
    """The services FBM's tools call, on a Flaim snapshot read through FakeFlaim."""
    from fbm.flaim.league import build_snapshot

    league = build_snapshot(reader(), CFG, mapper(), DAY, None, now=NOW).league
    fake = FakeFlaim()
    monkeypatch.setattr(svc, "_is_yahoo", lambda: True)
    monkeypatch.setattr(svc, "active_league", lambda: league)
    monkeypatch.setattr(svc, "_yahoo_client", lambda: reader(fake))
    monkeypatch.setattr(svc, "_conn", lambda: conn)
    monkeypatch.setattr("fbm.yahoo.ids.espn_players_from_db", lambda c: mapper().players)
    monkeypatch.setattr("fbm.yahoo.ids.load_overrides", lambda: {})
    return league, fake


def test_read_tools_work_on_the_flaim_route(live):
    league, fake = live
    board = svc.scoreboard_tool()
    mine = board["matchups"][0]
    assert mine["mine"] and mine["categories"][0]["values"]["Glass Cleaners"] == 0.482
    assert svc.standings_tool()["standings"][0]["team_key"] == OPP
    txs = svc.transactions_tool(10, "Bench Mob")
    assert txs["count"] == 1 and txs["transactions"][0]["players"][0]["player"] == "Coby White"
    offers = svc.pending_offers_tool()
    assert offers["incoming_trades"][0]["trader"] == "Bench Mob"
    assert offers["incoming_trades"][0]["trade_note"].startswith("Ignore previous")  # data, shown as data
    assert offers["waiver_claims"][0]["players"][0]["player"] == "Bennedict Mathurin"
    seen = svc.sync_pending_offers_tool()
    assert seen["pending_seen"] == 1 and seen["new"] == 1
    synced = svc.sync_transactions_tool(write_memory=False)
    assert synced["new"] == 4


# ------------------------------------------------------------------ health


def test_health_reports_the_flaim_sign_in():
    ok = health.check_yahoo("yahoo", {}, True, False, 10.0, "", 0, flaim={"signed_in": True, "refresh_days_left": 6.5})
    assert ok[0]["status"] == "OK" and "Flaim" in ok[0]["detail"]
    soon = health.check_yahoo("yahoo", {}, True, False, 10.0, "", 0, flaim={"signed_in": True, "refresh_days_left": 1})
    assert soon[0]["status"] == "WARN" and "fbm snapshot" in soon[0]["fix"]
    out = health.check_yahoo("yahoo", {"x": False}, True, False, 10.0, "boom", 0, flaim={"signed_in": False})
    assert out[0]["status"] == "FAIL" and out[0]["fix"] == "`fbm flaim login`"
    refresh = next(c for c in out if c["check"] == "Yahoo refresh")
    assert "fbm flaim login" in refresh["fix"]
    assert not any(c["check"] == "Yahoo credentials" for c in out)  # no Yahoo app needed on this route


def test_health_demo_source_names_the_flaim_route():
    out = health.check_yahoo("demo", {}, False, True, None, "", 0, flaim={"signed_in": True})
    source = next(c for c in out if c["check"] == "League source")
    assert "Flaim sign-in ready" in source["detail"] and "API approval" not in source["detail"]
    league = next(c for c in out if c["check"] == "League configured")
    assert "fbm flaim leagues --select" in league["fix"]


def test_health_flaim_status_reads_the_store(vault, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    assert health._flaim_status() == {"signed_in": False}
    oauth.save(signed_in(now=datetime.now(UTC).timestamp()))
    assert health._flaim_status()["signed_in"] is True
    monkeypatch.setenv("FBM_YAHOO_ROUTE", "auto")
    assert health._flaim_status() is None


# ------------------------------------------------------------------ fbm flaim check / dump


def test_check_reports_every_feed(conn):
    report = flaim_check.run(reader(), CFG, DAY, conn)
    feeds = {f.name: f for f in report.feeds}
    assert feeds["Your leagues"].status == "OK" and "Hardwood Classic" in feeds["Your leagues"].detail
    assert feeds["League info"].status == "OK" and "9/9 categories mapped" in feeds["League info"].detail
    assert feeds["Matchups (this week)"].status == "OK" and "no makes/attempts" in feeds["Matchups (this week)"].detail
    assert feeds["Your roster (today)"].detail.startswith("14 players")
    assert feeds["Pending trades & claims"].status == "OK"
    dry = feeds["Snapshot (dry run)"]
    assert dry.status == "WARN" and "players without an ESPN id" in dry.detail  # the empty test DB maps no one
    assert report.unmatched and not report.failed
    assert any("dates not sent by Flaim" in w for w in report.warnings)


def test_check_without_a_league_skips_league_feeds(conn):
    report = flaim_check.run(FlaimLeague(FakeFlaim(), week_windows={}), None, DAY, conn)
    assert report.feeds[0].status == "OK" and all(f.status == "SKIP" for f in report.feeds[1:])


def test_a_failing_feed_is_reported_not_raised(conn):
    def broken(args):
        raise YahooAPIError(500, "boom", "get_standings")

    report = flaim_check.run(reader(FakeFlaim({"get_standings": broken})), CFG, DAY, conn)
    assert next(f for f in report.feeds if f.name == "Standings").status == "FAIL" and report.failed


class RecordingFlaim(FakeFlaim):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def call_tool(self, name, args):
        payload = super().call_tool(name, args)
        self.recorder(name, args, payload)
        return payload


def test_cli_check_and_dump(tmp_path, monkeypatch):
    from fbm import cli_flaim
    from fbm.cli import app

    connect = db.connect
    monkeypatch.setattr("fbm.store.db.connect", lambda *a, **k: connect(":memory:"))
    monkeypatch.setattr(cli_flaim, "load_league_config", lambda: CFG)
    monkeypatch.setattr(cli_flaim, "_reader", lambda **kw: reader(RecordingFlaim(kw.get("recorder") or print)))
    result = runner.invoke(app, ["flaim", "check"])
    assert result.exit_code == 0, result.output
    assert "Snapshot (dry run)" in result.output and "Warnings" in result.output
    out = tmp_path / "dump"
    result = runner.invoke(app, ["flaim", "dump", "--out", str(out)])
    assert result.exit_code == 0, result.output
    files = sorted(p.name for p in out.iterdir())
    assert "check_report.json" in files and any(n.startswith("01_get_user_session") for n in files)
    sample = json.loads(next(out.glob("*_get_standings*.json")).read_text())
    assert sample["tool"] == "get_standings" and sample["payload"]["success"] is True
    assert zipfile.ZipFile(f"{out}.zip").namelist()
    assert "no passwords or tokens" in result.output


# ------------------------------------------------------------------ fbm flaim status / leagues / route / logout


def test_cli_status_never_prints_secrets(vault, monkeypatch, tmp_path):  # noqa: F811
    from fbm import cli_flaim
    from fbm.cli import app
    from fbm.flaim import league as flaim_league

    monkeypatch.setenv("FBM_YAHOO_ROUTE", "flaim")
    monkeypatch.setattr(flaim_league, "WARNINGS_PATH", tmp_path / "w.json")
    monkeypatch.setattr(cli_flaim, "load_league_config", lambda: CFG)
    state = signed_in(now=datetime.now(UTC).timestamp(), access_token="SECRET-AT", refresh_token="SECRET-RT")
    oauth.save(state)
    result = runner.invoke(app, ["flaim", "status"])
    assert result.exit_code == 0, result.output
    assert "Route: flaim" in result.output and "signed in" in result.output and "Hardwood Classic" in result.output
    assert "SECRET" not in result.output and "client-123" not in result.output
    result = runner.invoke(app, ["flaim", "logout"])
    assert "removed" in result.output and credentials.FLAIM_OAUTH not in vault
    assert "fbm flaim route session" in result.output


def test_cli_leagues_select_writes_league_toml(monkeypatch):
    from fbm import cli_flaim
    from fbm.cli import app

    saved: list[LeagueConfig] = []
    monkeypatch.setattr(cli_flaim, "_reader", lambda **kw: reader())
    monkeypatch.setattr(cli_flaim, "save_league_config", saved.append)
    result = runner.invoke(app, ["flaim", "leagues", "--select", "--league-id", "4242"])
    assert result.exit_code == 0, result.output
    assert saved == [LeagueConfig("466", "2026", LK, "Hardwood Classic", ME, "Glass Cleaners")]
    listing = runner.invoke(app, ["flaim", "leagues"])
    assert "Hardwood Classic" in listing.output and "Office League" not in listing.output
    missing = runner.invoke(app, ["flaim", "leagues", "--league-id", "999"])
    assert missing.exit_code == 1 and "isn't among your Flaim leagues" in missing.output


def test_cli_route_sets_the_route(monkeypatch):
    from fbm import cli_flaim
    from fbm.cli import app

    chosen: list[str] = []
    monkeypatch.setattr(cli_flaim, "save_route", chosen.append)
    assert runner.invoke(app, ["flaim", "route", "session"]).exit_code == 0 and chosen == ["session"]


def test_cli_route_and_status_survive_an_unknown_route(vault, monkeypatch):  # noqa: F811
    from fbm.cli import app

    monkeypatch.setenv("FBM_YAHOO_ROUTE", "carrier-pigeon")
    shown = runner.invoke(app, ["flaim", "route"])
    assert (
        shown.exit_code == 1
        and 'Unknown Yahoo route "carrier-pigeon"' in shown.output
        and "Traceback" not in shown.output
    )
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0 and "Unknown Yahoo route" in status.output


def test_cli_commands_need_a_sign_in(vault, monkeypatch):  # noqa: F811
    from fbm.cli import app

    result = runner.invoke(app, ["flaim", "check"])
    assert result.exit_code == 1 and "fbm flaim login" in result.output


def test_cli_login_saves_the_sign_in_picks_the_league_and_sets_the_route(vault, monkeypatch):  # noqa: F811
    from fbm import cli_flaim
    from fbm.cli import app

    saved, routes = [], []
    monkeypatch.setattr(cli_flaim, "_sign_in", lambda http, url, no_browser, timeout: signed_in(access_token="AT9"))
    monkeypatch.setattr(cli_flaim, "_reader", lambda **kw: reader())
    monkeypatch.setattr(cli_flaim, "save_league_config", saved.append)
    monkeypatch.setattr(cli_flaim, "save_route", routes.append)
    monkeypatch.setattr(cli_flaim, "_use_live_league", lambda: None)
    result = runner.invoke(app, ["flaim", "login", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert oauth.load().access_token == "AT9" and "AT9" not in result.output
    assert saved[0].league_key == LK and routes == ["flaim"]
    assert "reads your league through Flaim" in result.output


def test_cli_login_failure_saves_nothing(vault, monkeypatch):  # noqa: F811
    from fbm import cli_flaim
    from fbm.cli import app
    from fbm.flaim.errors import FlaimError

    def refused(*a):
        raise FlaimError("sign-in was not completed (access_denied)")

    monkeypatch.setattr(cli_flaim, "_sign_in", refused)
    result = runner.invoke(app, ["flaim", "login"])
    assert result.exit_code == 1 and "access_denied" in result.output and "Nothing saved" in result.output
    assert not vault
