"""Pure-logic tests for the planner: slot expression parsing + skeleton expansion."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.planner import (
    expand_preferred_slots,
    is_in_blackout,
    monday_of,
    parse_slot_expression,
    respect_min_gap,
)


# ----- parse_slot_expression -----


def test_parse_daily_form_emits_seven_days():
    out = parse_slot_expression("daily 19:00")
    assert len(out) == 7
    assert {d for d, _ in out} == set(range(7))
    assert all(t == time(19, 0) for _, t in out)


def test_parse_single_day():
    assert parse_slot_expression("Mon 12:00") == [(0, time(12, 0))]


def test_parse_csv_days():
    out = parse_slot_expression("Mon,Wed,Fri 19:00")
    assert sorted(d for d, _ in out) == [0, 2, 4]


def test_parse_weekdays():
    out = parse_slot_expression("weekdays 09:00")
    assert sorted(d for d, _ in out) == [0, 1, 2, 3, 4]


def test_parse_weekends():
    out = parse_slot_expression("weekends 11:00")
    assert sorted(d for d, _ in out) == [5, 6]


def test_parse_invalid_time_returns_empty():
    assert parse_slot_expression("Mon 25:00") == []
    assert parse_slot_expression("Mon noon") == []


def test_parse_unknown_day_returns_empty():
    assert parse_slot_expression("Funday 12:00") == []


# ----- expand_preferred_slots -----


def _strategy(timezone_name="UTC", quota=None, preferred=None, blackout=None):
    return SimpleNamespace(
        timezone=timezone_name,
        weekly_quota=quota or {},
        preferred_slots=preferred or {},
        blackout=blackout or {},
        min_gap_minutes={},
    )


def test_expand_respects_quota_capping():
    strategy = _strategy(
        quota={"ig_reels": 2},
        preferred={"ig_reels": ["Mon 19:00", "Tue 12:00", "Wed 18:00", "Thu 19:00"]},
    )
    week = date(2026, 5, 11)  # Monday
    out = expand_preferred_slots(strategy, week)
    assert len(out) == 2  # quota capped at 2
    assert all(preset == "ig_reels" for _, preset, _ in out)


def test_expand_skips_zero_quota():
    strategy = _strategy(
        quota={"ig_reels": 0, "tiktok": 1},
        preferred={"ig_reels": ["Mon 19:00"], "tiktok": ["Tue 20:00"]},
    )
    out = expand_preferred_slots(strategy, date(2026, 5, 11))
    assert len(out) == 1
    assert out[0][1] == "tiktok"


def test_expand_returns_utc_for_istanbul_strategy():
    strategy = _strategy(
        timezone_name="Europe/Istanbul",
        quota={"ig_reels": 1},
        preferred={"ig_reels": ["Mon 19:00"]},
    )
    out = expand_preferred_slots(strategy, date(2026, 5, 11))
    assert len(out) == 1
    dt, _, _ = out[0]
    assert dt.tzinfo == timezone.utc
    # Istanbul is UTC+3, so 19:00 local → 16:00 UTC.
    assert dt.hour == 16


def test_expand_falls_back_to_utc_for_unknown_timezone():
    strategy = _strategy(
        timezone_name="Atlantis/Sunken",
        quota={"ig_reels": 1},
        preferred={"ig_reels": ["Mon 12:00"]},
    )
    out = expand_preferred_slots(strategy, date(2026, 5, 11))
    assert out[0][0].hour == 12


def test_expand_sorts_by_scheduled_at():
    strategy = _strategy(
        quota={"ig_reels": 3},
        preferred={"ig_reels": ["Wed 09:00", "Mon 12:00", "Fri 18:00"]},
    )
    out = expand_preferred_slots(strategy, date(2026, 5, 11))
    times = [dt for dt, _, _ in out]
    assert times == sorted(times)


def test_expand_drops_invalid_expressions():
    strategy = _strategy(
        quota={"ig_reels": 5},
        preferred={"ig_reels": ["Mon 19:00", "garbage", "Tue 25:00", "Wed 18:00"]},
    )
    out = expand_preferred_slots(strategy, date(2026, 5, 11))
    assert len(out) == 2  # only valid expressions counted


# ----- is_in_blackout -----


def test_blackout_specific_day_window():
    blackout = {"Sat": ["00:00-08:00"]}
    sat_morning = datetime(2026, 5, 16, 5, 0, tzinfo=timezone.utc)  # Saturday 05:00 UTC
    assert is_in_blackout(sat_morning, blackout, ZoneInfo("UTC")) is True


def test_blackout_outside_window():
    blackout = {"Sat": ["00:00-08:00"]}
    sat_evening = datetime(2026, 5, 16, 20, 0, tzinfo=timezone.utc)
    assert is_in_blackout(sat_evening, blackout, ZoneInfo("UTC")) is False


def test_blackout_daily_window_applies_every_day():
    blackout = {"daily": ["02:00-04:00"]}
    monday_03 = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
    assert is_in_blackout(monday_03, blackout, ZoneInfo("UTC")) is True


def test_blackout_handles_malformed_window():
    blackout = {"Mon": ["junk"]}
    monday_noon = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    # Malformed entries are ignored — not a hard fail.
    assert is_in_blackout(monday_noon, blackout, ZoneInfo("UTC")) is False


# ----- respect_min_gap -----


def test_min_gap_zero_always_passes():
    assert respect_min_gap(datetime(2026, 5, 11, 12, tzinfo=timezone.utc), "ig_reels", [], {}) is True


def test_min_gap_blocks_when_within_window():
    a = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    b = datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc)  # 60 min after
    assert respect_min_gap(b, "ig_reels", [a], {"ig_reels": 120}) is False


def test_min_gap_allows_outside_window():
    a = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    b = datetime(2026, 5, 11, 16, 0, tzinfo=timezone.utc)  # 4h after
    assert respect_min_gap(b, "ig_reels", [a], {"ig_reels": 120}) is True


# ----- monday_of -----


def test_monday_of_monday_is_self():
    assert monday_of(date(2026, 5, 11)) == date(2026, 5, 11)


def test_monday_of_wednesday_is_prior_monday():
    assert monday_of(date(2026, 5, 13)) == date(2026, 5, 11)


def test_monday_of_sunday_is_prior_monday():
    assert monday_of(date(2026, 5, 17)) == date(2026, 5, 11)
