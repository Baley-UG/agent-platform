"""Pure-function tests for account_pool gating logic.

The DB-dependent paths (acquire / release SQL) are exercised in M4
end-to-end when we have a real Postgres. M3 tests cover the parts that
don't need a DB: active-hours window, role requirement, quota lookup
fallbacks.
"""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    """Test env. Forces a fresh Fernet key + a fresh module reload so
    settings pick up our environment without dragging in user .env."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.account_pool as ap

    importlib.reload(ap)
    return ap


def _account(**kwargs):
    """Build a SimpleNamespace that quacks enough like Account for the
    pure-function checks. Saves us from instantiating the real SQLModel
    (which would try to register on metadata)."""
    defaults = dict(
        id=uuid4(),
        username="anyone",
        timezone="UTC",
        active_hours_start=8,
        active_hours_end=23,
        weekday_pattern=127,  # all days
        role="scraper",
        status="active",
        quota_tier="warm",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_within_active_hours_default_window(_setup_env):
    ap = _setup_env
    # Wed 12:00 UTC — well inside the default 8–23 window.
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(_account(), now) is True


def test_outside_active_hours_default(_setup_env):
    ap = _setup_env
    now = datetime(2026, 5, 6, 3, 0, tzinfo=timezone.utc)  # 3 AM
    assert ap._within_active_hours(_account(), now) is False


def test_active_hours_respects_account_timezone(_setup_env):
    ap = _setup_env
    # 03:00 UTC == 06:00 in Istanbul. Our default window starts at 08
    # → still outside.
    now = datetime(2026, 5, 6, 3, 0, tzinfo=timezone.utc)
    acc = _account(timezone="Europe/Istanbul")
    assert ap._within_active_hours(acc, now) is False
    # 06:00 UTC == 09:00 in Istanbul → inside the window.
    later = datetime(2026, 5, 6, 6, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(acc, later) is True


def test_wrap_around_window(_setup_env):
    """Window 22..6 must mean 'late evening through early morning'."""
    ap = _setup_env
    acc = _account(active_hours_start=22, active_hours_end=6)
    midnight = datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(acc, midnight) is True
    morning = datetime(2026, 5, 6, 5, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(acc, morning) is True
    afternoon = datetime(2026, 5, 6, 14, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(acc, afternoon) is False
    late = datetime(2026, 5, 6, 23, 0, tzinfo=timezone.utc)
    assert ap._within_active_hours(acc, late) is True


def test_weekday_bitmap(_setup_env):
    """Weekend bits off → weekend timestamps must be rejected."""
    ap = _setup_env
    # Bits: Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64.
    # 1+2+4+8+16 = 31 → Mon-Fri only.
    acc = _account(weekday_pattern=31)
    wednesday = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)  # Wed
    saturday = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)  # Sat
    assert ap._within_active_hours(acc, wednesday) is True
    assert ap._within_active_hours(acc, saturday) is False


def test_required_role_for_job(_setup_env):
    ap = _setup_env
    job_canary = SimpleNamespace(params={"canary": True})
    job_regular = SimpleNamespace(params={"canary": False})
    job_unset = SimpleNamespace(params=None)
    assert ap._required_role_for_job(job_canary) == "canary"
    assert ap._required_role_for_job(job_regular) == "scraper"
    assert ap._required_role_for_job(job_unset) == "scraper"


def test_cooldown_seconds_ranges(_setup_env):
    ap = _setup_env
    rl = ap._cooldown_seconds("rate_limited")
    assert 2 * 3600 <= rl <= 4 * 3600
    sf = ap._cooldown_seconds("soft_fail")
    assert sf is not None
    assert ap._cooldown_seconds("challenge") is None
    assert ap._cooldown_seconds("fatal") is None
    assert ap._cooldown_seconds("success") is None
