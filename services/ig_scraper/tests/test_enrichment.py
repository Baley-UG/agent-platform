"""Pure-function tests for the enrichment promotion gate.

DB-side (UPSERT into ig_scan_targets) is exercised in M7 end-to-end
with a real Postgres. Here we just lock in the threshold logic so a
typo in IG_MIN_* won't silently flood the daily fleet.
"""

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IG_MIN_FOLLOWERS_FOR_ENRICH", "5000")
    monkeypatch.setenv("IG_MIN_MEDIA_FOR_ENRICH", "12")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.scrapers.enrichment as enrichment

    importlib.reload(enrichment)
    return enrichment


def test_promote_above_thresholds(_setup_env):
    profile = dict(follower_count=10_000, media_count=42, is_private=False)
    assert _setup_env._should_promote(profile, min_followers=5000, min_media=12) is True


def test_reject_below_followers(_setup_env):
    profile = dict(follower_count=4_999, media_count=42, is_private=False)
    assert _setup_env._should_promote(profile, min_followers=5000, min_media=12) is False


def test_reject_below_media(_setup_env):
    profile = dict(follower_count=100_000, media_count=11, is_private=False)
    assert _setup_env._should_promote(profile, min_followers=5000, min_media=12) is False


def test_reject_private(_setup_env):
    profile = dict(follower_count=100_000, media_count=500, is_private=True)
    assert _setup_env._should_promote(profile, min_followers=5000, min_media=12) is False


def test_missing_fields_treated_as_zero(_setup_env):
    profile = dict(is_private=False)
    assert _setup_env._should_promote(profile, min_followers=5000, min_media=12) is False
