"""Flaim payloads -> FBM's Yahoo read types. Fixtures (tests/fixtures/flaim/) follow Flaim's published tool schemas
for a synthetic 12-team Yahoo NBA 9-cat league; no test touches the network."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from fbm.flaim import reads
from fbm.flaim.errors import FlaimUnavailable
from fbm.flaim.reads import FlaimLeague
from fbm.yahoo.client import YahooAPIError
from fbm.yahoo.settings import summarize

FIX = Path(__file__).parent / "fixtures" / "flaim"
LK, ME, OPP = "466.l.4242", "466.l.4242.t.1", "466.l.4242.t.2"
DAY = date(2026, 11, 18)  # a Wednesday: week 5 = Mon 2026-11-16 .. Sun 2026-11-22


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def other_roster(team: str) -> dict:
    """A plausible roster for the ten teams without their own fixture (players unknown to ESPN)."""
    n = int(team.rsplit(".t.", 1)[-1])
    slots = ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util", "BN", "BN", "BN"]
    players = [
        {
            "playerKey": f"466.p.{8000 + n * 20 + k}",
            "name": f"Player {n}-{k}",
            "team": "BOS",
            "position": "SF,PF",
            "selectedPosition": slot,
            "eligiblePositions": ["SF", "PF", "F", "Util"],
        }
        for k, slot in enumerate(slots)
    ]
    return {"success": True, "data": {"teamKey": team, "snapshot": {"type": "current"}, "players": players}}


class FakeFlaim:
    """Stands in for FlaimClient: answers `call_tool` from the fixtures and records every call."""

    def __init__(self, overrides: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.overrides = overrides or {}
        self.closed = False

    def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        if name in self.overrides:
            found = self.overrides[name]
            return found(args) if callable(found) else copy.deepcopy(found)
        if name == "get_roster":
            team, day = args["team_id"], args.get("as_of_date")
            if team == ME:
                dated = f"get_roster_mine_{day}"
                return fixture(dated if day and (FIX / f"{dated}.json").exists() else "get_roster_mine")
            return fixture("get_roster_opponent") if team == OPP else other_roster(team)
        if name == "get_transactions":
            kind = {"waiver": "_waiver", "pending_trade": "_pending_trade"}.get(args.get("type"), "")
            return fixture(f"get_transactions{kind}")
        return fixture(name)

    def tools(self) -> list[str]:
        return [name for name, _ in self.calls]

    def close(self) -> None:
        self.closed = True


def reader(fake: FakeFlaim | None = None, **kwargs) -> FlaimLeague:
    return FlaimLeague(fake or FakeFlaim(), LK, 2026, ME, week_windows=kwargs.pop("week_windows", {}), **kwargs)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("fbm.timeutil.nba_today", lambda now=None: DAY)


# ------------------------------------------------------------------ leagues


def test_session_leagues_keeps_only_yahoo_basketball():
    refs = reads.session_leagues(fixture("get_user_session"))
    assert [(r.league_key, r.team_key, r.season_year, r.name) for r in refs] == [(LK, ME, 2026, "Hardwood Classic")]
    assert refs[0].game_key == "466"
    row = reads.discovery_row(refs[0])
    assert row["league_key"] == LK and row["season"] == "2026" and row["game_key"] == "466"


def test_flaim_league_discovery_matches_yahoo_discovery_shape():
    r = reader()
    assert r.leagues()[0]["name"] == "Hardwood Classic"
    assert r.my_teams() == [{"team_key": ME, "name": "Glass Cleaners"}]


def test_team_key_accepts_ids_and_keys():
    assert reads.team_key(LK, "3") == f"{LK}.t.3"
    assert reads.team_key(LK, f"{LK}.t.3") == f"{LK}.t.3"
    assert reads.team_key(LK, None) == ""


# ------------------------------------------------------------------ teams, standings


def test_teams_get_waiver_priority_from_standings():
    teams = reader().teams()
    assert len(teams) == 12
    mine = next(t for t in teams if t.team_key == ME)
    assert mine.is_mine and mine.name == "Glass Cleaners" and mine.manager == "Owner"
    assert mine.waiver_priority == 11  # rank 2 -> priority 11 in the fixture
    assert {t.waiver_priority for t in teams} == set(range(1, 13))
    assert mine.week_adds is None  # Flaim doesn't say: counted from transactions in the snapshot


def test_teams_fall_back_to_the_standings():
    r = reader(FakeFlaim({"get_league_info": {"success": True, "data": {"name": "No teams"}}}))
    teams = r.teams()
    assert len(teams) == 12 and next(t for t in teams if t.team_key == ME).name == "Glass Cleaners"


def test_standings_rows():
    rows = reader().standings()
    assert [r.rank for r in rows] == list(range(1, 13))
    top = rows[0]
    assert (top.team_key, top.wins, top.losses, top.ties, top.playoff_seed) == (OPP, 14, 4, 0, 1)
    assert top.pct == pytest.approx(0.778)
    assert rows[-1].playoff_seed is None


# ------------------------------------------------------------------ matchups


def test_matchups_per_side_category_rows():
    matchups = reader().scoreboard()
    assert len(matchups) == 6
    mine = next(m for m in matchups if m.team(ME))
    assert (mine.week, mine.status, mine.is_playoffs) == (5, "midevent", False)
    assert mine.week_start is None  # not in Flaim's schema: filled by the snapshot
    me, opp = mine.team(ME), mine.opponent_of(ME)
    assert opp.team_key == OPP
    assert me.stats["5"] == ".482" and me.stats["12"] == "301" and me.stats["19"] == "31"
    assert "9004003" not in me.stats  # percentages only
    assert me.points == 5
    assert mine.stat_winners["12"] == ME and mine.stat_winners["15"] == OPP and mine.stat_winners["17"] is None
    assert mine.stat_winners["19"] == ME  # fewer turnovers wins


def test_matchups_per_matchup_rows_and_labels():
    payload = {
        "success": True,
        "data": {
            "week": 7,
            "weekStart": "2026-11-30",
            "weekEnd": "2026-12-13",
            "matchups": [
                {
                    "home": {"teamId": "1", "teamName": "A"},
                    "away": {"teamId": "2", "teamName": "B"},
                    "status": "final",
                    "categories": [
                        {"displayName": "FGM/A", "home": "120/250", "away": "100/230"},
                        {"displayName": "FG%", "home": 0.48, "away": 0.435, "winner": "home"},
                        {"displayName": "TO", "values": {"home": 30, "away": 25}, "winner": "away"},
                        {"displayName": "ST", "home": 12, "away": 12, "winner": "tie"},
                    ],
                }
            ],
        },
    }
    (m,) = reads.parse_matchups(payload, LK)
    assert (m.week, m.status, m.week_start, m.week_end) == (7, "postevent", date(2026, 11, 30), date(2026, 12, 13))
    a = m.team(ME)
    assert a.stats == {"9004003": "120/250", "5": "0.48", "19": "30", "17": "12"}
    assert m.stat_winners == {"5": ME, "19": OPP, "17": None}  # display-only FGM/A never has a winner


def test_matchups_use_remembered_week_windows():
    windows = {5: (date(2026, 11, 16), date(2026, 11, 22))}
    mine = next(m for m in reader(week_windows=windows).scoreboard() if m.team(ME))
    assert (mine.week_start, mine.week_end) == windows[5]


def test_games_remaining_and_categories_seen():
    payload = fixture("get_matchups")
    assert reads.games_remaining(payload, LK, ME) == 33
    assert reads.games_remaining(payload, LK, "nope") is None
    seen = {c.stat_id for c in reads.matchup_categories(payload)}
    assert seen == {"5", "8", "10", "12", "15", "16", "17", "18", "19"}


# ------------------------------------------------------------------ settings


def test_settings_raw_summarizes_like_yahoo():
    summary = summarize(reader().settings_raw())
    assert summary["name"] == "Hardwood Classic" and summary["num_teams"] == "12"
    assert summary["current_week"] == "5" and summary["start_date"] == "2026-10-20"
    assert summary["settings"]["max_weekly_adds"] == "4" and summary["settings"]["waiver_rule"] == "continuous"
    assert summary["scoring_type_label"].startswith("H2H Categories")
    slots = {r["position"]: (r["count"], r["is_starting"]) for r in summary["roster_slots"]}
    assert slots["C"] == (2, True) and slots["Util"] == (2, True) and slots["BN"] == (3, False)
    assert slots["IL+"] == (1, False)
    cats = {c["stat_id"]: c["higher_is_better"] for c in summary["scoring_categories"]}
    assert set(cats) == {"5", "8", "10", "12", "15", "16", "17", "18", "19"} and cats["19"] is False
    assert summary["settings"]["waiver_time"] == "2"


def test_settings_raw_tolerates_missing_fields():
    raw = reads.settings_raw({"name": "Bare", "rosterPositions": ["PG", "C", "C", "BN", "IL"]})
    summary = summarize(raw)
    slots = {r["position"]: (r["count"], r["is_starting"]) for r in summary["roster_slots"]}
    assert slots == {"PG": (1, True), "C": (2, True), "BN": (1, False), "IL": (1, False)}
    assert summary["scoring_categories"] == []  # -> FBM's standard 9-cat
    r = reader(FakeFlaim({"get_league_info": {"success": True, "data": {"name": "Bare"}}}))
    r.settings_raw()
    assert any("roster positions" in w for w in r.warnings)


# ------------------------------------------------------------------ rosters


def test_current_roster_keeps_yahoo_eligibility_and_slots():
    roster = reader().roster(ME, DAY)
    assert len(roster.players) == 14 and roster.is_editable
    by_name = {p.name: p for p in roster.players}
    booker = by_name["Devin Booker"]
    assert booker.player_key == "466.p.6002" and booker.nba_team == "PHO"
    assert booker.eligible_positions == ("PG", "SG", "G", "Util") and booker.selected_position == "SG"
    assert by_name["Derrick White"].selected_position == "UTIL"
    assert by_name["Kristaps Porzingis"].selected_position == "IL" and by_name["Kristaps Porzingis"].status == "INJ"
    assert by_name["Nicolas Claxton"].status == "GTD"


def test_historical_roster_derives_eligibility_and_asks_by_date():
    fake = FakeFlaim()
    roster = reader(fake).roster(ME, date(2026, 11, 16), current=False)
    assert fake.calls[-1][1]["as_of_date"] == "2026-11-16"
    by_name = {p.name: p for p in roster.players}
    assert by_name["Jalen Williams"].selected_position == "BN"
    assert by_name["Mikal Bridges"].eligible_positions == ("SG", "SF", "G", "F")
    assert "IL" in by_name["Kristaps Porzingis"].eligible_positions  # injured -> IL eligible


def test_roster_without_slots_warns_and_benches():
    payload = {"success": True, "data": {"players": [{"playerId": "9", "name": "X", "position": "C"}]}}
    roster, warnings = reads.parse_roster(payload, ME, DAY, "466")
    assert roster.players[0].selected_position == "BN" and roster.players[0].player_key == "466.p.9"
    assert any("without a lineup slot" in w for w in warnings)


# ------------------------------------------------------------------ available players


def test_available_players_waivers_and_ownership():
    pool = reader().available()
    assert [p.player_id for p in pool] == ["6101", "6102", "6103", "6104"]
    mathurin = pool[0]
    assert mathurin.ownership_type == "waivers" and mathurin.waiver_date == date(2026, 11, 19)
    assert mathurin.percent_owned == 61 and mathurin.percent_owned_delta is None
    assert pool[1].ownership_type == "freeagents" and pool[1].status == "GTD"
    assert pool[3].ownership_type == "" and pool[3].percent_owned is None
    r = reader()
    assert [p.player_id for p in r.players(status="W")] == ["6101"]
    assert [p.player_id for p in r.players(status="FA")] == ["6102", "6103", "6104"]


# ------------------------------------------------------------------ transactions


def test_transactions_use_yahoo_words_and_attribute_teams():
    txs = reader().transactions(count=25)
    by_key = {t.key: t for t in txs}
    add_drop = by_key[f"{LK}.tr.140"]
    assert (add_drop.type, add_drop.status) == ("add/drop", "successful")
    assert add_drop.timestamp == datetime(2026, 11, 17, 15, 30, tzinfo=UTC)
    add, drop = add_drop.players
    assert (add.action, add.destination_team_key, add.source_type, add.player_key) == (
        "add",
        ME,
        "freeagents",
        "466.p.6013",
    )
    assert (drop.action, drop.source_team_key, drop.destination_type) == ("drop", ME, "waivers")
    claim = by_key[f"{LK}.tr.139"]
    assert claim.type == "add" and claim.players[0].source_type == "waivers"  # a waiver priority was used
    trade = by_key[f"{LK}.tr.138"]
    assert trade.type == "trade" and trade.status == "successful"
    legs = {p.name: (p.source_team_key, p.destination_team_key) for p in trade.players}
    assert legs == {"Alperen Sengun": (f"{LK}.t.4", f"{LK}.t.3"), "Zion Williamson": (f"{LK}.t.3", f"{LK}.t.4")}
    assert by_key[f"{LK}.tr.137"].type == "drop"


def test_transactions_have_no_offset_paging():
    fake = FakeFlaim()
    assert reader(fake).transactions(count=25, start=25) == []
    assert fake.calls == []


def test_pending_claims_and_offers():
    r = reader()
    txs = r.pending()
    claim = next(t for t in txs if t.type == "waiver")
    assert claim.waiver_team_key == ME and claim.status == "pending"
    offer = next(t for t in txs if t.type == "pending_trade")
    assert (offer.trader_team_key, offer.tradee_team_key, offer.status) == (OPP, ME, "proposed")
    legs = {p.name: (p.source_team_key, p.destination_team_key) for p in offer.players}
    assert legs["Josh Giddey"] == (OPP, ME) and legs["Derrick White"] == (ME, OPP)
    assert not r.warnings


def test_pending_trade_without_a_proposer_is_flagged():
    payload = fixture("get_transactions_pending_trade")
    row = payload["data"]["transactions"][0]
    del row["trader_team_key"], row["tradee_team_key"]
    r = reader(FakeFlaim({"get_transactions": lambda a: payload if a.get("type") == "pending_trade" else {}}))
    (offer,) = [t for t in r.pending() if t.type == "pending_trade"]
    assert offer.trader_team_key == "" and offer.tradee_team_key == ""
    assert any("without a known proposer" in w for w in r.warnings)


# ------------------------------------------------------------------ the reader


def test_reader_caches_league_reads_and_records_warnings():
    fake = FakeFlaim()
    r = reader(fake)
    r.teams()
    r.standings()
    r.settings_raw()
    r.scoreboard()
    assert fake.tools().count("get_league_info") == 1 and fake.tools().count("get_standings") == 1
    assert fake.tools().count("get_matchups") == 1
    args = fake.calls[0][1]
    assert args == {"platform": "yahoo", "sport": "basketball", "league_id": LK, "season_year": 2026}


def test_unsupported_yahoo_path_degrades_with_a_clear_error():
    with pytest.raises(FlaimUnavailable) as err:
        reader().get("league/466.l.4242/teams/stats;type=season")
    assert isinstance(err.value, YahooAPIError)  # every Yahoo read handler handles it
    assert "not available with the Flaim route" in str(err.value)


def test_reader_without_league_says_how_to_pick_one():
    with pytest.raises(YahooAPIError, match="no league selected"):
        FlaimLeague(FakeFlaim(), week_windows={}).standings()


def test_season_comes_from_the_session_for_another_league():
    fake = FakeFlaim()
    r = FlaimLeague(fake, "", None, "", week_windows={})
    r.standings(LK)
    assert fake.calls[-1] == ("get_standings", {"platform": "yahoo", "sport": "basketball", "league_id": LK,
                                                "season_year": 2026})  # fmt: skip
