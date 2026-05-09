"""Budget enforcement helpers — pure logic where it doesn't touch DB."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.budget import day_start_utc, has_rule_budget_remaining, has_weekly_budget_remaining, week_start_utc


def test_week_start_utc_is_monday_midnight():
    out = week_start_utc(datetime(2026, 5, 15, 14, 30, tzinfo=timezone.utc))  # Friday
    assert out == datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)


def test_week_start_utc_for_monday_is_self_midnight():
    out = week_start_utc(datetime(2026, 5, 11, 14, 30, tzinfo=timezone.utc))
    assert out == datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)


def test_day_start_utc_normalizes_time():
    out = day_start_utc(datetime(2026, 5, 11, 14, 30, 25, tzinfo=timezone.utc))
    assert out == datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc)


def test_has_weekly_budget_remaining_no_cap_passes():
    project = SimpleNamespace(id="p", weekly_budget_cap_usd=None)
    # Even with a fake session that would error, no cap → True without checking.
    assert has_weekly_budget_remaining(None, project) is True


def test_has_rule_budget_remaining_no_cap_passes():
    assert has_rule_budget_remaining(None, "p", None) is True
