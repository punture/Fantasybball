"""`fbm sim` must default to this week's real opponent, not the next team in the demo rotation."""

import sqlite3
from types import SimpleNamespace

from typer.testing import CliRunner

from fbm import cli
from fbm.analytics import simulation, week
from fbm.analytics.simulation import MatchupOdds
from fbm.league.model import FantasyTeam, LeagueState


def _league() -> LeagueState:
    teams = [FantasyTeam(f"l.t.{i}", f"Team {i}") for i in range(1, 5)]
    return LeagueState("l", "League", 2027, "l.t.1", teams, opponent_key="l.t.3", source="yahoo")


def test_sim_defaults_to_this_weeks_opponent(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE games (et_date TEXT, season_type INTEGER)")
    ctx = SimpleNamespace(conn=conn, value_map=dict, models=lambda ids: {}, return_dates=dict)
    planned = []

    def plan(team, *args, **kwargs):
        planned.append(team.name)
        return SimpleNamespace(player_games=[]), None

    monkeypatch.setattr(cli, "_load_league", lambda demo=False: _league())
    monkeypatch.setattr(cli, "_context", lambda: ctx)
    monkeypatch.setattr(week, "plan_team_week", plan)
    monkeypatch.setattr(week, "teams_playing", lambda conn, a, b: {})
    monkeypatch.setattr(
        simulation,
        "simulate_matchup",
        lambda *a, **k: MatchupOdds(categories=(), expected_score=0.0, p_majority=0.0, cats_won_hist=()),
    )
    result = CliRunner().invoke(cli.app, ["sim", "--start", "2026-11-16"])
    assert result.exit_code == 0, result.output
    assert planned == ["Team 1", "Team 3"]
    assert "Team 1 vs Team 3" in " ".join(result.output.split())
