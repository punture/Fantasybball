"""Dashboard helpers: the Matchup page's Opponent picker opens on this week's opponent."""

import pytest

from fbm.dashboard.helpers import opponent_choices


def _overview(opponent: str | None) -> dict:
    return {
        "my_team_key": "t.2",
        "this_week_opponent": opponent,
        "teams": [
            {"key": "t.1", "name": "Bench Mob", "manager": "a"},
            {"key": "t.2", "name": "My Team", "manager": "me"},
            {"key": "t.3", "name": "Euro Steppers", "manager": "b"},
            {"key": "t.4", "name": "Zone Busters", "manager": "c"},
        ],
    }


def test_picker_lists_every_other_team_and_opens_on_this_weeks_opponent():
    teams, index = opponent_choices(_overview("Euro Steppers"))
    assert teams == {"Bench Mob": "t.1", "Euro Steppers": "t.3", "Zone Busters": "t.4"}
    assert list(teams)[index] == "Euro Steppers"


@pytest.mark.parametrize("opponent", [None, "Renamed Team"])
def test_picker_opens_on_the_first_team_when_the_opponent_is_unknown(opponent):
    teams, index = opponent_choices(_overview(opponent))
    assert list(teams)[index] == "Bench Mob"
