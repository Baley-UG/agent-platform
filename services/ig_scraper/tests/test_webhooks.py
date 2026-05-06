"""Webhook signing + filter + backoff tests (no DB needed)."""

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.webhooks as webhooks

    importlib.reload(webhooks)
    return webhooks


def test_sign_with_secret(_setup_env):
    sig = _setup_env._sign("topsecret", b'{"hello":"world"}')
    assert sig is not None
    assert sig.startswith("sha256=")
    # Determinism: same input → same signature.
    assert sig == _setup_env._sign("topsecret", b'{"hello":"world"}')


def test_sign_without_secret(_setup_env):
    assert _setup_env._sign(None, b"anything") is None
    assert _setup_env._sign("", b"anything") is None


def test_sign_changes_with_body(_setup_env):
    a = _setup_env._sign("k", b"a")
    b = _setup_env._sign("k", b"b")
    assert a != b


def test_backoff_schedule(_setup_env):
    """First retry quick, later retries slower, capped."""
    s1 = _setup_env._backoff_seconds(1)
    s3 = _setup_env._backoff_seconds(3)
    # Attempt 1 = 30s; doubles each step. Eventually capped at
    # _BACKOFF_MAX_SECONDS (6 hours). Attempt 30 is well past the cap.
    s_huge = _setup_env._backoff_seconds(30)
    assert s1 == 30
    assert s3 > s1
    assert s_huge == _setup_env._BACKOFF_MAX_SECONDS


def test_filter_match_min_score_passes(_setup_env):
    assert _setup_env._matches_filters({"min_score": 70}, {"score": 85}) is True


def test_filter_match_min_score_blocks(_setup_env):
    assert _setup_env._matches_filters({"min_score": 90}, {"score": 80}) is False


def test_filter_match_extra_key(_setup_env):
    assert _setup_env._matches_filters({"event": "x"}, {"event": "x"}) is True
    assert _setup_env._matches_filters({"event": "x"}, {"event": "y"}) is False


def test_filter_no_filter_always_matches(_setup_env):
    assert _setup_env._matches_filters(None, {}) is True
    assert _setup_env._matches_filters({}, {"score": 0}) is True
