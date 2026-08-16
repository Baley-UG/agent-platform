"""RQ enqueue helpers.

Single connection cached at module level. The worker uses its own Queue
instances; this module is for the API/services layer to dispatch work.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from redis import Redis
from rq import Queue
from rq.job import Job

from app.core.config import settings
from app.core.metrics import cp_jobs_total


# Per-queue job timeouts (seconds). RQ's default 180s is fine for cheap
# LLM/intake calls but blows up on ffmpeg compose — a 12s 1080×1350
# slideshow with `zoompan` + libx264 medium routinely takes 4-7 minutes
# on CPU. Generous defaults; tune via env later if needed.
_QUEUE_TIMEOUTS = {
    "analyzer": 600,        # vision LLM with multi-image inputs
    # Brand-asset auto-tagging — one vision LLM call per asset, no
    # async polling. 5-10s typical; 5min cap for tail latency.
    "brand_asset_tag": 300,
    # Director — one BIG vision LLM call: reference frames + ~20 brand
    # asset thumbs in a single prompt. Heavier than the tagger; 10min
    # cap for tail latency on slow models.
    "director": 600,
    "image_gen": 600,       # fal.ai async polling
    "video_gen": 900,       # Seedance image-to-video (long polls)
    "audio_gen": 300,       # ElevenLabs TTS
    "media_render": 1800,   # ffmpeg compose — slowest stage
    # Phase 3 — ffmpeg keyframe extraction. Fast for short reels
    # (5-30s typical, completes in 5-15s) but scene-detect on a long
    # video can chew minutes. 20min cap.
    "frame_extract": 1200,
    # Repurpose — one ffmpeg process cuts every keep segment for an
    # aspect group in a single decode pass. Same order of magnitude as
    # compose, so it gets the same cap.
    "segment_cut": 1800,
    "publish": 900,         # IG/TT container polling
    "planner": 300,
}


@lru_cache(maxsize=1)
def _conn() -> Redis:
    return Redis.from_url(settings.redis_url)


def enqueue(queue_name: str, func_path: str, *args: Any, **kwargs: Any) -> Job:
    """Enqueue a job by importable function path (e.g. 'app.workers.analyzer.run').

    Using a dotted path rather than the function object keeps API and worker
    decoupled — the API doesn't import worker code at request time.

    Each known queue gets a generous per-queue `job_timeout` from
    `_QUEUE_TIMEOUTS`; callers can override via an explicit
    `job_timeout=...` kwarg.
    """
    queue = Queue(queue_name, connection=_conn())
    kwargs.setdefault("job_timeout", _QUEUE_TIMEOUTS.get(queue_name, 600))
    job = queue.enqueue(func_path, *args, **kwargs)
    cp_jobs_total.labels(queue=queue_name, task=func_path).inc()
    return job
