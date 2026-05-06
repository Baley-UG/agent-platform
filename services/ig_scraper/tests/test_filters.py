"""Pre-fetch filter tests."""

from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.filters as filters

    importlib.reload(filters)
    return filters


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_no_thresholds_passes(_setup_env):
    r = _setup_env.passes_filter(
        like_count=10, play_count=None, view_count=None,
        taken_at=_now(),
        min_likes=None, min_impressions=None, since=None,
    )
    assert r.passed is True


def test_min_likes_blocks(_setup_env):
    r = _setup_env.passes_filter(
        like_count=10, play_count=None, view_count=None,
        taken_at=_now(),
        min_likes=100, min_impressions=None, since=None,
    )
    assert r.passed is False
    assert r.reason == "below_min_likes"


def test_min_impressions_uses_play_count_first(_setup_env):
    r = _setup_env.passes_filter(
        like_count=1000, play_count=5_000, view_count=None,
        taken_at=_now(),
        min_likes=None, min_impressions=10_000, since=None,
    )
    assert r.passed is False
    assert r.reason == "below_min_impressions"


def test_min_impressions_falls_back_to_view_count(_setup_env):
    r = _setup_env.passes_filter(
        like_count=1000, play_count=None, view_count=20_000,
        taken_at=_now(),
        min_likes=None, min_impressions=10_000, since=None,
    )
    assert r.passed is True


def test_photo_only_with_min_impressions_skipped(_setup_env):
    """Plan § 7: photo posts (no play/view counts) skip when min_impressions is set."""
    r = _setup_env.passes_filter(
        like_count=1_000_000, play_count=None, view_count=None,
        taken_at=_now(),
        min_likes=None, min_impressions=1, since=None,
    )
    assert r.passed is False
    assert r.reason == "below_min_impressions"


def test_since_blocks_old_posts(_setup_env):
    cutoff = _now() - timedelta(days=7)
    old = _now() - timedelta(days=30)
    r = _setup_env.passes_filter(
        like_count=1, play_count=None, view_count=None,
        taken_at=old,
        min_likes=None, min_impressions=None, since=cutoff,
    )
    assert r.passed is False
    assert r.reason == "before_since"
