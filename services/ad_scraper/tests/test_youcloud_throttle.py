"""Tests for the process-wide rate gate.

Clock and sleep are injected, so these assert on the pacing arithmetic and
spend no wall-clock time. A fake clock that only advances when someone
sleeps is exactly the invariant we want to check: it proves the gate's
decisions come from elapsed time, not from luck.
"""

import asyncio

import pytest

from app.core.config import settings
from app.services.youcloud.client import YouCloudClient
from app.services.youcloud.errors import RateLimited
from app.services.youcloud.throttle import Throttle, reset_shared_throttle, shared_throttle


class FakeClock:
    """A monotonic clock that advances only when someone sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.slept.append(delay)
        self.now += delay


def _gate(clock, **kwargs):
    kwargs.setdefault("min_interval", 2.0)
    kwargs.setdefault("max_interval", 20.0)
    kwargs.setdefault("cooldown", 30.0)
    kwargs.setdefault("jitter_ratio", 0.0)
    return Throttle(clock=clock.time, sleep=clock.sleep, rand=lambda: 0.0, **kwargs)


class TestPacing:
    async def test_first_request_does_not_wait(self):
        clock = FakeClock()
        waited = await _gate(clock).acquire()
        assert waited == 0.0
        assert clock.slept == []

    async def test_second_request_waits_the_interval(self):
        clock = FakeClock()
        gate = _gate(clock)
        await gate.acquire()
        waited = await gate.acquire()
        assert waited == pytest.approx(2.0)

    async def test_a_caller_that_arrives_late_does_not_wait(self):
        """Pacing is a floor on spacing, not a fixed cadence — a job that took
        longer than the interval to come back must not be penalised."""
        clock = FakeClock()
        gate = _gate(clock)
        await gate.acquire()
        clock.now += 60.0
        assert await gate.acquire() == 0.0

    async def test_concurrent_callers_are_spaced_not_batched(self):
        """The whole reason this module exists: with AD_WORKER_CONCURRENCY=2
        two jobs used to pace independently and push twice as hard. Five
        concurrent callers through one gate must come out 2s apart."""
        clock = FakeClock()
        gate = _gate(clock)
        stamps = []

        async def caller():
            await gate.acquire()
            stamps.append(clock.now)

        await asyncio.gather(*[caller() for _ in range(5)])
        assert stamps == [1000.0, 1002.0, 1004.0, 1006.0, 1008.0]

    async def test_zero_interval_is_a_free_pass(self):
        """Used by the other test modules, so it has to genuinely not sleep."""
        clock = FakeClock()
        gate = _gate(clock, min_interval=0.0, max_interval=0.0, cooldown=0.0)
        for _ in range(10):
            assert await gate.acquire() == 0.0
        assert clock.slept == []

    async def test_jitter_only_ever_adds(self):
        clock = FakeClock()
        gate = Throttle(
            min_interval=2.0,
            max_interval=20.0,
            cooldown=30.0,
            jitter_ratio=0.5,
            clock=clock.time,
            sleep=clock.sleep,
            rand=lambda: 1.0,  # worst case
        )
        await gate.acquire()
        assert await gate.acquire() == pytest.approx(3.0), "2.0 * (1 + 0.5)"


class TestPenalty:
    async def test_penalty_widens_the_interval(self):
        clock = FakeClock()
        gate = _gate(clock)
        assert gate.penalise() == pytest.approx(4.0)
        assert gate.penalise() == pytest.approx(8.0)

    async def test_penalty_is_capped(self):
        clock = FakeClock()
        gate = _gate(clock, max_interval=6.0)
        for _ in range(10):
            gate.penalise()
        assert gate.interval == pytest.approx(6.0)

    async def test_penalty_pauses_siblings_not_just_the_refused_caller(self):
        """The load-bearing behaviour. Job A gets rate-limited; job B, which
        is mid-run in the same process, must wait out the cooldown too —
        otherwise B keeps hammering the endpoint that just said slow down."""
        clock = FakeClock()
        gate = _gate(clock, cooldown=30.0)
        await gate.acquire()  # job A sends
        gate.penalise()  # ...and is refused
        waited = await gate.acquire()  # job B arrives
        assert waited == pytest.approx(30.0)

    async def test_penalty_can_grow_from_a_zero_floor(self):
        """A deployment with pacing switched off still has to be able to back
        off when the upstream refuses; multiplying zero would never grow."""
        clock = FakeClock()
        gate = _gate(clock, min_interval=0.0, cooldown=30.0)
        assert gate.penalise() == pytest.approx(20.0), "seeded from the cooldown, capped by max_interval"

    async def test_counts_penalties(self):
        clock = FakeClock()
        gate = _gate(clock)
        gate.penalise()
        gate.penalise()
        assert gate.penalties == 2


class TestRecovery:
    async def test_relaxes_after_a_run_of_clean_responses(self):
        clock = FakeClock()
        gate = _gate(clock, recovery_after=3, recovery_factor=0.5)
        gate.penalise()  # 2.0 -> 4.0
        for _ in range(2):
            gate.record_success()
        assert gate.interval == pytest.approx(4.0), "not yet — the run is not long enough"
        gate.record_success()
        assert gate.interval == pytest.approx(2.0)

    async def test_never_relaxes_below_the_floor(self):
        clock = FakeClock()
        gate = _gate(clock, recovery_after=1, recovery_factor=0.1)
        gate.penalise()
        for _ in range(20):
            gate.record_success()
        assert gate.interval == pytest.approx(2.0)

    async def test_a_penalty_resets_the_recovery_run(self):
        """Otherwise alternating success/refusal would drift back to the floor
        while the upstream is still refusing."""
        clock = FakeClock()
        gate = _gate(clock, recovery_after=2, recovery_factor=0.5)
        gate.penalise()  # -> 4.0
        gate.record_success()
        gate.penalise()  # -> 8.0, run reset
        gate.record_success()
        assert gate.interval == pytest.approx(8.0), "one success is not a run of two"


class TestSharedInstance:
    def test_shared_gate_is_a_singleton(self):
        reset_shared_throttle()
        try:
            assert shared_throttle() is shared_throttle()
        finally:
            reset_shared_throttle()

    def test_shared_gate_reads_the_configured_floor(self, monkeypatch):
        reset_shared_throttle()
        monkeypatch.setattr(settings, "AD_API_MIN_REQUEST_INTERVAL_SECONDS", 7.5)
        try:
            assert shared_throttle().interval == pytest.approx(7.5)
        finally:
            reset_shared_throttle()


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _rate_limited():
    return {
        "errors": [{"extensions": {"c": "00:400998", "m": "High visiting frequency, please try again later"}}],
        "data": None,
    }


def _ok():
    return {"data": {"materialList": {"page": 1, "total": 10, "limit": 50, "data": [{"material": {"id": "m1"}}]}}}


class TestClientIntegration:
    """The client's half: a rate limit must widen the gate and be retried on
    its own, larger budget."""

    def _client(self, gate):
        async def provider():
            return "cookie"

        return YouCloudClient(session_provider=provider, throttle=gate)

    async def test_rate_limit_widens_the_gate_and_retries(self, monkeypatch):
        clock = FakeClock()
        gate = _gate(clock)
        client = self._client(gate)
        responses = [_FakeResponse(_rate_limited()), _FakeResponse(_ok())]

        async def fake_post(*args, **kwargs):
            return responses.pop(0)

        monkeypatch.setattr(client._http, "post", fake_post)
        data = await client.execute("query {}", {}, operation_name="materialList")

        assert data["materialList"]["total"] == 10
        assert gate.penalties == 1
        assert gate.interval == pytest.approx(4.0)
        assert 30.0 in clock.slept, "the retry must wait out the cooldown"
        await client.aclose()

    async def test_rate_limit_uses_its_own_budget_not_the_transport_one(self, monkeypatch):
        """A rate limit must not be capped by AD_API_MAX_RETRIES — failing the
        job requeues it at page_from, spending *more* requests upstream."""
        clock = FakeClock()
        gate = _gate(clock)
        client = self._client(gate)
        monkeypatch.setattr(settings, "AD_API_MAX_RETRIES", 1)
        monkeypatch.setattr(settings, "AD_API_RATE_LIMIT_MAX_RETRIES", 4)
        calls = []

        async def fake_post(*args, **kwargs):
            calls.append(1)
            return _FakeResponse(_rate_limited())

        monkeypatch.setattr(client._http, "post", fake_post)
        with pytest.raises(RateLimited):
            await client.execute("query {}", {}, operation_name="materialList")
        assert len(calls) == 4, "spent the rate-limit budget, not the transport one"
        await client.aclose()

    async def test_client_reports_what_it_waited(self, monkeypatch):
        """Surfaced onto the job row: without it, "paced" and "slow" are
        indistinguishable in wall-clock time."""
        clock = FakeClock()
        gate = _gate(clock)
        client = self._client(gate)

        async def fake_post(*args, **kwargs):
            return _FakeResponse(_ok())

        monkeypatch.setattr(client._http, "post", fake_post)
        for _ in range(3):
            await client.execute("query {}", {}, operation_name="materialList")
        # First request passes free; the next two each wait the interval.
        assert client.waited_seconds == pytest.approx(4.0)
        await client.aclose()

    async def test_a_clean_run_leaves_the_gate_alone(self, monkeypatch):
        clock = FakeClock()
        gate = _gate(clock)
        client = self._client(gate)

        async def fake_post(*args, **kwargs):
            return _FakeResponse(_ok())

        monkeypatch.setattr(client._http, "post", fake_post)
        for _ in range(3):
            await client.execute("query {}", {}, operation_name="materialList")
        assert gate.penalties == 0
        assert gate.interval == pytest.approx(2.0)
        await client.aclose()
