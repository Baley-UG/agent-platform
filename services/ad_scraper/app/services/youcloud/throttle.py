"""Process-wide pacing for YouCloud requests.

The upstream answers `00:400998` — "High visiting frequency, please try
again later" — when we ask too fast. Retrying after that error is table
stakes; the point of this module is to hit it rarely in the first place.

Why it has to be process-wide, not per-client
---------------------------------------------
Before this, the only pacing was a sleep between pages inside
`paginate_materials`, so each job spaced *its own* requests. With
`AD_WORKER_CONCURRENCY=2` two jobs run in one process, each with its own
client and its own sleep, so the actual rate upstream saw was double the
configured one — and raising concurrency raised it further, silently. A
single shared gate makes the rate a property of the process, so
concurrency changes how many jobs make progress, not how hard we push.

Design
------
* One `asyncio.Lock` serialises the check-sleep-stamp step, so two
  coroutines cannot both conclude it is their turn. Waiters queue in FIFO
  order and each re-reads the clock, so N concurrent callers are spaced
  `interval` apart rather than all firing at once.
* `penalise()` implements the "additive-increase/multiplicative-decrease"
  half that matters here: on a refusal the interval widens geometrically
  up to a ceiling, and the shared gate is pushed out by a cooldown so
  **sibling jobs pause too** — not just the coroutine that got refused.
  That is the difference between backing off and merely retrying.
* `record_success()` walks the interval back down after a run of clean
  responses, so one bad minute does not slow the service until restart.
* Jitter only ever *adds* delay. Two worker containers (compose
  `replicas`) share no state, so without it their requests drift into
  lockstep and arrive in pairs.

Clock and sleep are injected so the tests can drive this in zero
wall-clock time; production passes neither and gets the event loop's own
monotonic clock.
"""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_throttle_interval_seconds

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


async def _loop_sleep(delay: float) -> None:
    """Default sleeper — the event loop's own."""
    await asyncio.sleep(delay)


def _loop_clock() -> float:
    """Default clock — monotonic, so a wall-clock jump cannot un-pace us."""
    return asyncio.get_event_loop().time()


class Throttle:
    """A shared rate gate. Construct once per process; see `shared_throttle`."""

    def __init__(
        self,
        *,
        min_interval: float,
        max_interval: float,
        cooldown: float,
        penalty_factor: float = 2.0,
        recovery_factor: float = 0.75,
        recovery_after: int = 8,
        jitter_ratio: float = 0.25,
        clock: Optional[Clock] = None,
        sleep: Optional[Sleeper] = None,
        rand: Optional[Callable[[], float]] = None,
    ) -> None:
        """Build a gate. `min_interval` is the floor; `max_interval` the ceiling."""
        self._min_interval = max(0.0, min_interval)
        self._max_interval = max(self._min_interval, max_interval)
        self._cooldown = max(0.0, cooldown)
        self._penalty_factor = max(1.0, penalty_factor)
        self._recovery_factor = min(1.0, max(0.1, recovery_factor))
        self._recovery_after = max(1, recovery_after)
        self._jitter_ratio = max(0.0, jitter_ratio)

        self._clock = clock or _loop_clock
        self._sleep = sleep or _loop_sleep
        self._rand = rand or random.random

        self._interval = self._min_interval
        self._next_allowed = 0.0
        self._successes = 0
        self._penalties = 0
        self._waited_total = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # State, for logs, metrics and assertions
    # ------------------------------------------------------------------

    @property
    def interval(self) -> float:
        """Current spacing floor, in seconds. Grows on refusals."""
        return self._interval

    @property
    def penalties(self) -> int:
        """How many refusals this gate has absorbed."""
        return self._penalties

    @property
    def waited_total(self) -> float:
        """Cumulative seconds callers have spent waiting at this gate."""
        return self._waited_total

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------

    async def acquire(self) -> float:
        """Wait until it is safe to send. Returns the seconds actually waited.

        The lock is held across the sleep on purpose: that is what makes the
        gate global. Callers pass through one at a time, each spaced by the
        current interval, so the process-wide rate is `1 / interval`
        regardless of how many jobs are running.
        """
        if self._max_interval <= 0.0 and self._cooldown <= 0.0:
            return 0.0
        async with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0:
                self._waited_total += wait
                await self._sleep(wait)
                now = self._clock()
            else:
                wait = 0.0
            self._next_allowed = now + self._spaced_interval()
            return wait

    def _spaced_interval(self) -> float:
        """The current interval plus jitter. Jitter never subtracts."""
        if self._interval <= 0.0 or self._jitter_ratio <= 0.0:
            return self._interval
        return self._interval * (1.0 + self._jitter_ratio * self._rand())

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def penalise(self, *, reason: str = "rate_limited") -> float:
        """Widen the gate after a refusal. Returns the new interval.

        Two effects, and the second is the one that actually helps: the
        interval widens for future requests, AND the shared gate is pushed
        out by `cooldown` so every other job in this process waits out the
        same pause. Backing off only the refused coroutine would leave its
        sibling hammering the endpoint that just said "slow down".
        """
        self._penalties += 1
        self._successes = 0
        previous = self._interval
        # A zero floor (tests, or pacing switched off) still has to be able
        # to grow, so seed from the cooldown rather than multiplying zero.
        base = previous if previous > 0 else min(self._cooldown, self._max_interval)
        self._interval = min(self._max_interval, max(base, previous * self._penalty_factor))
        if self._cooldown > 0:
            self._next_allowed = max(self._next_allowed, self._clock() + self._cooldown)
        logger.warning(
            "ad_throttle_penalised",
            reason=reason,
            interval_before=round(previous, 3),
            interval_after=round(self._interval, 3),
            cooldown_seconds=self._cooldown,
            penalties=self._penalties,
        )
        return self._interval

    def record_success(self) -> None:
        """Note a clean response; relax the gate after a run of them.

        Without this the first rate limit of the day would keep the service
        at its widest interval until the container restarted.
        """
        if self._interval <= self._min_interval:
            return
        self._successes += 1
        if self._successes < self._recovery_after:
            return
        self._successes = 0
        previous = self._interval
        self._interval = max(self._min_interval, self._interval * self._recovery_factor)
        logger.info(
            "ad_throttle_relaxed",
            interval_before=round(previous, 3),
            interval_after=round(self._interval, 3),
        )


# ----------------------------------------------------------------------
# Process-wide instance
# ----------------------------------------------------------------------

_shared: Optional[Throttle] = None


def shared_throttle() -> Throttle:
    """The one gate every client in this process shares.

    Built lazily so `settings` is read after any test monkeypatching, and
    so importing this module never touches the event loop.
    """
    global _shared
    if _shared is None:
        _shared = Throttle(
            min_interval=settings.AD_API_MIN_REQUEST_INTERVAL_SECONDS,
            max_interval=settings.AD_API_MAX_REQUEST_INTERVAL_SECONDS,
            cooldown=settings.AD_API_RATE_LIMIT_COOLDOWN_SECONDS,
            jitter_ratio=settings.AD_API_JITTER_RATIO,
        )
        # Publish the floor immediately. Otherwise the gauge reads 0 until the
        # first request, which on a dashboard looks like "no pacing at all".
        ad_throttle_interval_seconds.set(_shared.interval)
        logger.info(
            "ad_throttle_initialised",
            min_interval=_shared.interval,
            max_interval=settings.AD_API_MAX_REQUEST_INTERVAL_SECONDS,
            cooldown_seconds=settings.AD_API_RATE_LIMIT_COOLDOWN_SECONDS,
        )
    return _shared


def reset_shared_throttle() -> None:
    """Drop the shared gate. For tests, and for nothing else."""
    global _shared
    _shared = None
