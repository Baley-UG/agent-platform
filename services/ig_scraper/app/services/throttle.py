"""Anti-detection throttling primitives (plan § 5.3).

`human_delay(action)` is the only entry point a scraper needs.
Internally it picks the right per-action range from settings and sleeps
for a lognormal-jittered duration clipped to that range. A `Throttle`
instance can also accumulate call counts and inject macro-pauses every
N actions — that's used by long-running scrapes (full backfill).

Why lognormal, not uniform?
    Uniform delays produce a flat distribution that's itself a
    fingerprint. A real user's "interval-between-clicks" histogram
    looks lognormal-ish: most pauses are short, some are very long
    ("got distracted"). We approximate that with a clipped lognormal.
"""

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Literal

from app.core.config import settings
from app.core.logging import logger

ActionKind = Literal["feed", "post", "profile", "story", "hashtag", "login"]


def _range_for(action: ActionKind) -> tuple[float, float]:
    """Pull the (min, max) pair for `action` from settings.

    Centralised so a future test can monkey-patch the settings without
    re-implementing the lookup.
    """
    return {
        "feed": (settings.IG_DELAY_FEED_MIN, settings.IG_DELAY_FEED_MAX),
        "post": (settings.IG_DELAY_POST_MIN, settings.IG_DELAY_POST_MAX),
        "profile": (settings.IG_DELAY_PROFILE_MIN, settings.IG_DELAY_PROFILE_MAX),
        "story": (settings.IG_DELAY_STORY_MIN, settings.IG_DELAY_STORY_MAX),
        "hashtag": (settings.IG_DELAY_HASHTAG_MIN, settings.IG_DELAY_HASHTAG_MAX),
        "login": (settings.IG_DELAY_LOGIN_MIN, settings.IG_DELAY_LOGIN_MAX),
    }[action]


def lognormal_clipped(low: float, high: float) -> float:
    """Sample a lognormal value clipped to [low, high].

    The mean of the underlying normal is set so the median lands ~30%
    below `high`, matching the "most actions slightly slow, occasional
    longer pause" pattern noted in the plan.
    """
    if high <= low:
        return max(low, 0.0)
    target_median = low + (high - low) * 0.7
    mu = math.log(max(target_median, 0.05))
    sigma = 0.35  # ~30% spread; tighter than user-typing histograms
    sample = random.lognormvariate(mu, sigma)
    return max(low, min(high, sample))


async def human_delay(action: ActionKind) -> float:
    """Sleep for a lognormal-clipped duration appropriate for `action`.

    Returns the actual seconds slept (handy for tests and metrics).
    """
    low, high = _range_for(action)
    seconds = lognormal_clipped(low, high)
    await asyncio.sleep(seconds)
    return seconds


async def micro_jitter() -> float:
    """Tiny extra wobble between consecutive items inside a loop.

    Even per-action delays are too regular in aggregate without this —
    a real user introduces 0.5–2s of randomness between scrolling to
    the next post and tapping it.
    """
    seconds = random.uniform(settings.IG_MICRO_JITTER_MIN, settings.IG_MICRO_JITTER_MAX)
    await asyncio.sleep(seconds)
    return seconds


@dataclass
class Throttle:
    """Per-session throttling state.

    Holds the call counter and the next-macro-pause threshold so the
    scraper can call `await throttle.maybe_macro_pause()` after each
    action without owning the bookkeeping itself.
    """

    calls_made: int = 0
    next_macro_pause_at: int = field(default=0)
    long_break_used: bool = False

    def __post_init__(self) -> None:
        self._reset_macro_threshold()

    def _reset_macro_threshold(self) -> None:
        delta = random.randint(
            settings.IG_MACRO_PAUSE_EVERY_MIN, settings.IG_MACRO_PAUSE_EVERY_MAX
        )
        self.next_macro_pause_at = self.calls_made + delta

    async def after_action(self, action: ActionKind) -> None:
        """Standard per-action delay + micro-jitter + maybe macro-pause."""
        await human_delay(action)
        await micro_jitter()
        self.calls_made += 1
        await self.maybe_macro_pause()

    async def maybe_macro_pause(self) -> None:
        """Insert a macro-pause if we've hit the threshold.

        Once per session (probability `IG_LONG_BREAK_PROBABILITY`) the
        macro-pause is replaced with a much longer 5–15min break — the
        "user put the phone down" pattern.
        """
        if self.calls_made < self.next_macro_pause_at:
            return

        if (
            not self.long_break_used
            and random.random() < settings.IG_LONG_BREAK_PROBABILITY
        ):
            seconds = random.uniform(
                settings.IG_LONG_BREAK_SECONDS_MIN, settings.IG_LONG_BREAK_SECONDS_MAX
            )
            self.long_break_used = True
            kind = "long_break"
        else:
            seconds = random.uniform(
                settings.IG_MACRO_PAUSE_SECONDS_MIN, settings.IG_MACRO_PAUSE_SECONDS_MAX
            )
            kind = "macro_pause"

        logger.info("throttle_pause", kind=kind, seconds=round(seconds, 1), calls=self.calls_made)
        await asyncio.sleep(seconds)
        self._reset_macro_threshold()
