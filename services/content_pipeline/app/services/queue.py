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
    # Remake ffmpeg steps: probe / scene_detect / frame_extract / cut /
    # normalize / compose. Compose is the slowest; a single cap covers all.
    "remake_ffmpeg": 1800,
    # Remake AI steps: fal video-to-video / inpaint / i2v / whisper /
    # tts / lipsync / upscale — all async-polled, long tails.
    "remake_ai": 900,
    # Remake analysis: vision tagging + plan authoring (LLM).
    "remake_analysis": 600,
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
