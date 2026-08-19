"""The mirror stage's bounded concurrency.

Downloads are the slow half of a mirroring job — 4.46s per creative measured
against the live CDN, so a full page of 50 is close to four minutes of pure
waiting. They now go out `AD_MIRROR_CONCURRENCY` at a time.

The win is real but modest, and the tests should not imply otherwise:
throughput measured 2.63 MB/s at 1 concurrent, 3.80 at 4 and 2.80 at 8. The
constraint is bandwidth, not latency, so this buys roughly 1.2-1.6x rather
than Nx — which is exactly why the bound exists instead of an unbounded
`gather`.
"""

import asyncio

import pytest

from app.core.config import settings
from app.services import ingest


class _Tracker:
    """Records how many transfers are in flight at once."""

    def __init__(self, delay=0.01):
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self.calls = []

    async def transfer_async(self, *, material_id, media_url, poster_url):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        self.calls.append(material_id)
        try:
            await asyncio.sleep(self.delay)
            return f"key/{material_id}/media", f"key/{material_id}/poster"
        finally:
            self.inflight -= 1


def _pending(n):
    return [(f"m{i}", f"https://cdn/{i}.mp4", f"https://cdn/{i}.jpg") for i in range(n)]


@pytest.fixture()
def no_db(monkeypatch):
    """`persist_keys` needs a Session; the concurrency is what is under test."""
    import contextlib

    @contextlib.contextmanager
    def fake_scope():
        yield object()

    monkeypatch.setattr(ingest, "session_scope", fake_scope)
    monkeypatch.setattr(ingest.mirror, "persist_keys", lambda *a, **k: None)


class TestBoundedConcurrency:
    async def test_never_exceeds_the_configured_bound(self, monkeypatch, no_db):
        tracker = _Tracker()
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 3)
        monkeypatch.setattr(ingest.mirror, "transfer_async", tracker.transfer_async)
        stats = ingest.IngestStats()

        await ingest._mirror_page(_pending(12), stats=stats)

        assert tracker.peak == 3, f"peaked at {tracker.peak}, bound was 3"
        assert stats.mirrored == 12

    async def test_actually_overlaps(self, monkeypatch, no_db):
        """A bound of 1 would also 'never exceed 3' — prove work overlaps."""
        tracker = _Tracker()
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 4)
        monkeypatch.setattr(ingest.mirror, "transfer_async", tracker.transfer_async)

        await ingest._mirror_page(_pending(8), stats=ingest.IngestStats())

        assert tracker.peak > 1, "downloads ran one at a time — the gather is not working"

    async def test_a_bound_of_one_is_sequential(self, monkeypatch, no_db):
        tracker = _Tracker()
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 1)
        monkeypatch.setattr(ingest.mirror, "transfer_async", tracker.transfer_async)

        await ingest._mirror_page(_pending(5), stats=ingest.IngestStats())

        assert tracker.peak == 1

    async def test_every_creative_is_attempted_exactly_once(self, monkeypatch, no_db):
        tracker = _Tracker()
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 4)
        monkeypatch.setattr(ingest.mirror, "transfer_async", tracker.transfer_async)

        await ingest._mirror_page(_pending(10), stats=ingest.IngestStats())

        assert sorted(tracker.calls) == sorted(f"m{i}" for i in range(10))


class TestFailureIsolation:
    async def test_one_raising_transfer_does_not_lose_the_others(self, monkeypatch, no_db):
        """`transfer` swallows its own errors today, but a future change must
        not be able to fail a whole page."""
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 4)

        async def boom(*, material_id, media_url, poster_url):
            if material_id == "m2":
                raise RuntimeError("socket exploded")
            return "k/media", None

        monkeypatch.setattr(ingest.mirror, "transfer_async", boom)
        stats = ingest.IngestStats()

        await ingest._mirror_page(_pending(5), stats=stats)

        assert stats.mirrored == 4
        assert stats.mirror_failed == 1

    async def test_empty_keys_count_as_a_failure(self, monkeypatch, no_db):
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 2)

        async def nothing(*, material_id, media_url, poster_url):
            return None, None

        monkeypatch.setattr(ingest.mirror, "transfer_async", nothing)
        stats = ingest.IngestStats()

        await ingest._mirror_page(_pending(3), stats=stats)

        assert stats.mirrored == 0
        assert stats.mirror_failed == 3


class TestTiming:
    async def test_records_how_long_downloading_took(self, monkeypatch, no_db):
        """Surfaced on the job row: mirroring dominates a job's wall clock, so
        'slow job' and 'slow downloads' need to be distinguishable."""
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 4)
        monkeypatch.setattr(ingest.mirror, "transfer_async", _Tracker(delay=0.02).transfer_async)
        stats = ingest.IngestStats()

        await ingest._mirror_page(_pending(4), stats=stats)

        assert stats.mirror_seconds > 0
        assert stats.as_dict()["mirror_seconds"] == stats.mirror_seconds

    async def test_accumulates_across_pages(self, monkeypatch, no_db):
        monkeypatch.setattr(settings, "AD_MIRROR_CONCURRENCY", 4)
        monkeypatch.setattr(ingest.mirror, "transfer_async", _Tracker(delay=0.01).transfer_async)
        stats = ingest.IngestStats()

        await ingest._mirror_page(_pending(2), stats=stats)
        first = stats.mirror_seconds
        await ingest._mirror_page(_pending(2), stats=stats)

        assert stats.mirror_seconds > first, "second page overwrote the first page's time"
