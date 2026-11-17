"""Pure helpers for the Streamlit dashboard. app.py renders its pages on import, so logic worth testing lives here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def opponent_choices(overview: Mapping[str, Any]) -> tuple[dict[str, str], int]:
    """The Matchup page's Opponent picker, from `league_overview()`: {team name: team key} for every team but mine,
    and the index to open on: this week's opponent, else the first team (demo league, offseason, or a week Yahoo
    hasn't paired yet)."""
    teams = {t["name"]: t["key"] for t in overview["teams"] if t["key"] != overview["my_team_key"]}
    names = list(teams)
    current = overview.get("this_week_opponent")
    return teams, names.index(current) if current in names else 0
