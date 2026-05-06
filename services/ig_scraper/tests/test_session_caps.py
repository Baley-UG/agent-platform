"""Session cap tests (M10).

Throttle.session_should_end() decides whether the scraper should end
its session early. The reads are simple enough we test them with no
mocking — just patch settings + monotonic clock.
"""

import time as time_module

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("IG_SESSION_MAX_CALLS", "5")
    monkeypatch.setenv("IG_SESSION_MAX_MINUTES", "1")
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.throttle as throttle

    importlib.reload(throttle)
    return throttle


def test_fresh_session_doesnt_end(_setup_env):
    t = _setup_env.Throttle()
    assert t.session_should_end() is False


def test_call_cap_triggers(_setup_env):
    t = _setup_env.Throttle()
    t.calls_made = 5
    assert t.session_should_end() is True


def test_below_call_cap_does_not_trigger(_setup_env):
    t = _setup_env.Throttle()
    t.calls_made = 4
    assert t.session_should_end() is False


def test_minute_cap_triggers(_setup_env, monkeypatch):
    """Mock monotonic so we can time-travel without real waiting."""
    base = time_module.monotonic()
    t = _setup_env.Throttle()
    monkeypatch.setattr(time_module, "monotonic", lambda: base + 70)
    assert t.session_should_end() is True
