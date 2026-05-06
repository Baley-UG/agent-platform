"""Tests for the M8 median-score gate inside enrichment._should_promote."""

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IG_MIN_FOLLOWERS_FOR_ENRICH", "5000")
    monkeypatch.setenv("IG_MIN_MEDIA_FOR_ENRICH", "12")
    monkeypatch.setenv("IG_MIN_SCORE_FOR_ENRICH", "50")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.scrapers.enrichment as enrichment

    importlib.reload(enrichment)
    return enrichment


def _good_profile(**overrides):
    return {
        "follower_count": 10_000,
        "media_count": 50,
        "is_private": False,
        **overrides,
    }


def test_no_score_data_passes(_setup_env):
    """Sample-too-small (median_score=None) must not block promotion."""
    assert _setup_env._should_promote(
        _good_profile(), min_followers=5000, min_media=12,
        median_score=None, min_score=50,
    ) is True


def test_high_score_passes(_setup_env):
    assert _setup_env._should_promote(
        _good_profile(), min_followers=5000, min_media=12,
        median_score=72.5, min_score=50,
    ) is True


def test_low_score_blocks(_setup_env):
    """Big account, low quality → don't track them daily."""
    assert _setup_env._should_promote(
        _good_profile(follower_count=500_000), min_followers=5000, min_media=12,
        median_score=21.0, min_score=50,
    ) is False


def test_score_gate_disabled_when_min_score_none(_setup_env):
    """If env doesn't set the threshold, the gate is a no-op."""
    assert _setup_env._should_promote(
        _good_profile(), min_followers=5000, min_media=12,
        median_score=10.0, min_score=None,
    ) is True


def test_gate_still_requires_followers_and_media(_setup_env):
    """Score gate doesn't override the deterministic gates."""
    assert _setup_env._should_promote(
        _good_profile(follower_count=100), min_followers=5000, min_media=12,
        median_score=99.0, min_score=50,
    ) is False
    assert _setup_env._should_promote(
        _good_profile(media_count=2), min_followers=5000, min_media=12,
        median_score=99.0, min_score=50,
    ) is False
