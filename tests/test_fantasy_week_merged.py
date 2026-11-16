"""The calendar fallback for Yahoo matchup weeks honors Yahoo's merged (two-week) periods."""

from datetime import date

from fbm.analytics.week import fantasy_week
from fbm.flaim.league import calendar_week


def test_regular_weeks_run_monday_to_sunday():
    assert fantasy_week(date(2026, 11, 18)) == (date(2026, 11, 16), date(2026, 11, 22))
    assert fantasy_week(date(2026, 11, 22)) == (date(2026, 11, 16), date(2026, 11, 22))


def test_nba_cup_and_all_star_weeks_span_two_weeks():
    cup = (date(2026, 11, 30), date(2026, 12, 13))
    assert fantasy_week(date(2026, 11, 30)) == cup
    assert fantasy_week(date(2026, 12, 9)) == cup
    assert fantasy_week(date(2026, 12, 14)) == (date(2026, 12, 14), date(2026, 12, 20))
    assert fantasy_week(date(2027, 2, 24)) == (date(2027, 2, 15), date(2027, 2, 28))


def test_flaim_calendar_fallback_uses_the_merged_week():
    assert calendar_week(date(2026, 12, 2)) == (date(2026, 11, 30), date(2026, 12, 13))
    # opening week starts on opening night, not the Monday before
    assert calendar_week(date(2026, 10, 22), date(2026, 10, 20)) == (date(2026, 10, 20), date(2026, 10, 25))
