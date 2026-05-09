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


@lru_cache(maxsize=1)
def _conn() -> Redis:
    return Redis.from_url(settings.redis_url)


def enqueue(queue_name: str, func_path: str, *args: Any, **kwargs: Any) -> Job:
    """Enqueue a job by importable function path (e.g. 'app.workers.analyzer.run').

    Using a dotted path rather than the function object keeps API and worker
    decoupled — the API doesn't import worker code at request time.
    """
    queue = Queue(queue_name, connection=_conn())
    job = queue.enqueue(func_path, *args, **kwargs)
    cp_jobs_total.labels(queue=queue_name, task=func_path).inc()
    return job
