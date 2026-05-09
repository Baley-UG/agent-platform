"""Intake rule matching engine."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.intake_rules import matches


def _rule(conditions):
    return SimpleNamespace(conditions=conditions)


def test_min_score_pass():
    assert matches(_rule({"min_score": 70}), {"score": 75}) is True


def test_min_score_fail():
    assert matches(_rule({"min_score": 70}), {"score": 65}) is False


def test_min_score_missing_value_fails_closed():
    assert matches(_rule({"min_score": 70}), {}) is False


def test_max_duration_pass_and_fail():
    rule = _rule({"max_duration_sec": 60})
    assert matches(rule, {"duration_sec": 30}) is True
    assert matches(rule, {"duration_sec": 90}) is False


def test_must_have_caption():
    rule = _rule({"must_have_caption": True})
    assert matches(rule, {"has_caption": True}) is True
    assert matches(rule, {"has_caption": False}) is False
    assert matches(rule, {}) is False


def test_media_types_membership():
    rule = _rule({"media_types": ["reel", "video"]})
    assert matches(rule, {"media_type": "reel"}) is True
    assert matches(rule, {"media_type": "photo"}) is False


def test_posted_within_days_recent_passes():
    candidate = {"posted_at": datetime.now(timezone.utc) - timedelta(days=2)}
    assert matches(_rule({"posted_within_days": 7}), candidate) is True


def test_posted_within_days_too_old_fails():
    candidate = {"posted_at": datetime.now(timezone.utc) - timedelta(days=30)}
    assert matches(_rule({"posted_within_days": 7}), candidate) is False


def test_unknown_condition_keys_are_ignored():
    """Forward-compat: tomorrow's PR may add a new rule key; stale rules shouldn't fail closed."""
    assert matches(_rule({"future_unknown_key": 42}), {"score": 0}) is True


def test_combined_conditions_and_logic():
    rule = _rule(
        {
            "min_score": 50,
            "must_have_caption": True,
            "media_types": ["reel"],
            "max_duration_sec": 60,
        }
    )
    assert (
        matches(
            rule,
            {"score": 75, "has_caption": True, "media_type": "reel", "duration_sec": 30},
        )
        is True
    )
    # One condition fails → whole rule fails.
    assert (
        matches(
            rule,
            {"score": 75, "has_caption": True, "media_type": "photo", "duration_sec": 30},
        )
        is False
    )
