"""Worker process — claims ingestion jobs and runs them.

Architecture:
- N concurrent loops (`AD_WORKER_CONCURRENCY`), each claiming its own job
  via `SELECT ... FOR UPDATE SKIP LOCKED`. Scaling out is `replicas: 2+`
  in compose; SKIP LOCKED makes that safe with no code change.
- Graceful shutdown on SIGTERM / SIGINT: loops finish the job in hand
  before exiting.
- On startup, jobs left `running` by a previous container that died are
  requeued. That is when stuck rows actually appear, so it beats a timer.

Cross-session ORM safety: the claim step captures a plain `JobSnapshot`
dataclass rather than carrying an ORM row across `session_scope`
boundaries. Same pattern as ig_scraper's worker, same reason —
DetachedInstance errors are otherwise a matter of time.

Error mapping is the interesting part. YouCloud failures are already
classified by `app.services.youcloud.errors`, and each class implies a
different job outcome:

    AuthExpired   → terminal, AND the credential row is marked rejected so
                    the cached token is dropped and the dashboard says why.
                    Only an operator can mint a new token, so retrying would
                    burn the budget to reach the same conclusion later.
    PlanDenied    → terminal. Retrying cannot change the account's plan.
    BadFilter     → terminal. The filter is wrong; a human must fix it.
    TransientError→ retry with the job's remaining attempts.
    TransportError→ retry.

Run with: `python -m app.worker`.
"""

import asyncio
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_job_duration_seconds, start_worker_metrics_server
from app.services import credentials as creds
from app.services import ingest, jobs
from app.services.database import session_scope
from app.services.youcloud.errors import (
    AuthExpired,
    BadFilter,
    PlanDenied,
    TransientError,
    TransportError,
    YouCloudError,
)

PROCESS_NAME = "worker"

# Failures that mean "a human has to change something" — retrying just
# burns the attempt budget and delays the operator seeing the real reason.
_TERMINAL_ERRORS = (AuthExpired, PlanDenied, BadFilter)


@dataclass
class JobSnapshot:
    """Plain-Python view of a claimed job.

    Lives across `session_scope()` boundaries without DetachedInstance
    drama; every later session re-fetches by id when it needs the live row.
    """

    id: uuid.UUID
    filters: Dict[str, Any]
    page_from: int
    page_to: int
    order: str
    mirror: Optional[bool]
    attempt: int
    max_attempts: int


async def _process_one_job(loop_id: int) -> bool:
    """Claim and run one job. Returns False when the queue was empty."""
    with session_scope() as session:
        job = jobs.claim_next_job(session)
        snapshot = (
            JobSnapshot(
                id=job.id,
                filters=dict(job.filters or {}),
                page_from=job.page_from,
                page_to=job.page_to,
                order=job.order,
                mirror=job.mirror,
                attempt=job.attempt,
                max_attempts=job.max_attempts,
            )
            if job is not None
            else None
        )
    if snapshot is None:
        return False

    job_id = snapshot.id
    started = datetime.now(timezone.utc)
    logger.info(
        "ad_job_started",
        loop_id=loop_id,
        job_id=str(job_id),
        attempt=snapshot.attempt,
        page_from=snapshot.page_from,
        page_to=snapshot.page_to,
    )

    try:
        stats = await ingest.run_job(
            job_id=job_id,
            filters=snapshot.filters,
            page_from=snapshot.page_from,
            page_to=snapshot.page_to,
            order=snapshot.order,
            job_mirror=snapshot.mirror,
        )
    except AuthExpired as exc:
        # Record the rejection on the credential too: that drops the cached
        # token, bumps consecutive_failures, and flips the row to
        # `login_failed` once it is clearly dead — so /ready and the panel
        # point at the token rather than at the job.
        with session_scope() as session:
            creds.mark_rejected(session, str(exc))
            jobs.mark_failed(session, job_id, str(exc), error_code=getattr(exc, "code", None))
        return True
    except _TERMINAL_ERRORS as exc:
        with session_scope() as session:
            jobs.mark_failed(session, job_id, str(exc), error_code=getattr(exc, "code", None))
        return True
    except (TransientError, TransportError) as exc:
        with session_scope() as session:
            jobs.mark_retry(session, job_id, str(exc), error_code=getattr(exc, "code", None))
        return True
    except YouCloudError as exc:
        # Unknown YouCloud failure — retry rather than give up, on the same
        # reasoning as `errors.classify`'s unknown-code fallback.
        with session_scope() as session:
            jobs.mark_retry(session, job_id, str(exc), error_code=getattr(exc, "code", None))
        return True
    except Exception as exc:  # noqa: BLE001 — a bug here must not kill the worker
        logger.exception("ad_job_unhandled_exception", loop_id=loop_id, job_id=str(job_id))
        with session_scope() as session:
            jobs.mark_retry(session, job_id, f"{type(exc).__name__}: {exc}")
        return True
    finally:
        ad_job_duration_seconds.observe((datetime.now(timezone.utc) - started).total_seconds())

    with session_scope() as session:
        jobs.mark_succeeded(session, job_id, stats.as_dict())
    return True


async def _worker_loop(loop_id: int, shutdown: asyncio.Event) -> None:
    """Drive one worker loop until shutdown.

    Sleeps between empty polls so an idle queue doesn't hammer the DB with
    SKIP LOCKED queries.
    """
    logger.info("ad_worker_loop_started", loop_id=loop_id)
    idle_sleep = max(0.5, settings.AD_WORKER_POLL_SECONDS)
    while not shutdown.is_set():
        try:
            did_work = await _process_one_job(loop_id)
        except Exception:  # noqa: BLE001 — outer safety net
            logger.exception("ad_worker_loop_unhandled", loop_id=loop_id)
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=idle_sleep)
            except asyncio.TimeoutError:
                continue
    logger.info("ad_worker_loop_stopping", loop_id=loop_id)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Catch SIGTERM/SIGINT and signal graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _trigger():
        if not shutdown.is_set():
            logger.info("ad_worker_shutdown_signal")
            shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _trigger)
        except NotImplementedError:
            # Windows or restricted environments — fall back silently.
            pass


async def main() -> None:
    """Process entry point."""
    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    # Before anything else: this process owns the interesting counters, and
    # an unscraped counter is indistinguishable from a broken one.
    start_worker_metrics_server()

    concurrency = max(1, settings.AD_WORKER_CONCURRENCY)
    logger.info(
        "ad_worker_starting",
        concurrency=concurrency,
        mirror_policy=settings.AD_MIRROR_MEDIA,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    # Heal rows abandoned by a worker that died mid-job.
    try:
        with session_scope() as session:
            jobs.requeue_stuck_jobs(session)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logger.warning("ad_worker_requeue_on_start_failed", error=str(exc))

    tasks = [asyncio.create_task(_worker_loop(i, shutdown), name=f"ad-worker-{i}") for i in range(concurrency)]

    try:
        await shutdown.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("ad_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
