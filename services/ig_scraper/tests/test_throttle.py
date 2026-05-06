"""Anti-detection throttle tests.

We don't actually wait for real-world delays in tests — patch
asyncio.sleep so the assertions run instantly.
"""

import asyncio
import statistics

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("IG_SECRET_KEY", Fernet.generate_key().decode())
    import importlib

    import app.core.config as cfg

    importlib.reload(cfg)
    import app.services.throttle as throttle

    importlib.reload(throttle)
    return throttle


@pytest.mark.asyncio
async def test_human_delay_within_action_range(_setup_env, monkeypatch):
    """Sampled delays must always land inside the configured range."""
    samples: list[float] = []

    async def _capture(seconds: float) -> None:
        samples.append(seconds)

    monkeypatch.setattr(_setup_env.asyncio, "sleep", _capture)

    for _ in range(200):
        await _setup_env.human_delay("feed")

    assert all(_setup_env.settings.IG_DELAY_FEED_MIN <= s <= _setup_env.settings.IG_DELAY_FEED_MAX for s in samples)
    # Lognormal with sigma=0.35 should give a real spread, not the same number 200x.
    assert statistics.pstdev(samples) > 0.5


def test_lognormal_clipped_obeys_bounds(_setup_env):
    for _ in range(500):
        v = _setup_env.lognormal_clipped(2.0, 5.0)
        assert 2.0 <= v <= 5.0


@pytest.mark.asyncio
async def test_macro_pause_fires_after_threshold(_setup_env, monkeypatch):
    """Throttle.maybe_macro_pause should sleep once we cross the threshold."""
    sleeps: list[float] = []

    async def _capture(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_setup_env.asyncio, "sleep", _capture)

    t = _setup_env.Throttle()
    t.next_macro_pause_at = 1
    t.calls_made = 1
    await t.maybe_macro_pause()
    # Threshold reset → next_macro_pause_at advanced past calls_made.
    assert t.next_macro_pause_at > 1
    # At least one sleep happened, and it landed in either the macro
    # range (30–180) or the long-break range (300–900).
    assert sleeps
    last = sleeps[-1]
    assert (
        _setup_env.settings.IG_MACRO_PAUSE_SECONDS_MIN <= last <= _setup_env.settings.IG_MACRO_PAUSE_SECONDS_MAX
    ) or (
        _setup_env.settings.IG_LONG_BREAK_SECONDS_MIN <= last <= _setup_env.settings.IG_LONG_BREAK_SECONDS_MAX
    )
