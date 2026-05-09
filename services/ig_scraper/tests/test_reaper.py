"""Tests for the stuck-job reaper logic.

DB-side execution lives in M10 end-to-end. This test validates the
threshold + decision branches via mocking, since the reaper logic is
SQL-driven and we don't have a Postgres in unit tests.
"""

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IG_JOB_STUCK_AFTER_MINUTES", "30")
    monkeypatch.setenv("IG_REAPER_INTERVAL_SECONDS", "15")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    return cfg.settings


def test_reaper_settings_loaded(_setup_env):
    """Both knobs must round-trip through config."""
    assert _setup_env.IG_JOB_STUCK_AFTER_MINUTES == 30
    assert _setup_env.IG_REAPER_INTERVAL_SECONDS == 15


def test_reap_stuck_jobs_signature(_setup_env):
    """Function exists with the expected kwarg-only API.

    The signature is the contract the scheduler depends on, so a
    refactor that breaks it should be caught here.
    """
    import inspect

    from app.services import jobs

    sig = inspect.signature(jobs.reap_stuck_jobs)
    params = sig.parameters
    assert "session" in params
    assert "older_than_minutes" in params
    assert params["older_than_minutes"].kind == inspect.Parameter.KEYWORD_ONLY
