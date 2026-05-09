"""Worker process — claims jobs and runs them.

Architecture:
- N concurrent worker tasks (`IG_WORKER_CONCURRENCY`), each running a
  `_worker_loop()`. Each task independently claims its own job via
  SKIP LOCKED, then claims its own account from the pool.
- One heartbeat task that touches `ig_worker_heartbeat` every
  `IG_HEARTBEAT_INTERVAL_SECONDS`.
- Graceful shutdown on SIGTERM / SIGINT — the main task sets a
  shutdown event; loops finish their current job before exiting.

Cross-session ORM safety:
- The worker pulls a `JobSnapshot` (plain dataclass) from the claim
  step instead of carrying an ORM `ScrapeJob` across `session_scope`
  boundaries. Detached-instance pitfalls disappear; every subsequent
  session re-fetches by id when it needs the live row.

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
from app.models.job import ScrapeJob
from app.services import account_pool, jobs, scrapers, usage
from app.services.database import session_scope
from app.services.heartbeat import beat, make_instance_id

PROCESS_NAME = "worker"
INSTANCE_ID = make_instance_id()


@dataclass
class JobSnapshot:
    """Plain-Python view of a claimed job.

    Lives across `session_scope()` boundaries without DetachedInstance
    drama. The handful of attributes the worker actually needs are
    captured at claim time; everything else is re-fetched in-session.
    """

    id: uuid.UUID
    job_type: str
    target: str
    params: Dict[str, Any]
    min_likes: Optional[int]
    min_impressions: Optional[int]
    scan_target_id: Optional[uuid.UUID]
    attempt: int
    max_attempts: int

    @classmethod
    def from_orm(cls, job: ScrapeJob) -> "JobSnapshot":
        return cls(
            id=job.id,
            job_type=job.job_type,
            target=job.target,
            params=dict(job.params or {}),
            min_likes=job.min_likes,
            min_impressions=job.min_impressions,
            scan_target_id=job.scan_target_id,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
        )


async def _heartbeat_loop(shutdown: asyncio.Event) -> None:
    """Touch the heartbeat row every IG_HEARTBEAT_INTERVAL_SECONDS."""
    interval = settings.IG_HEARTBEAT_INTERVAL_SECONDS
    logger.info("worker_heartbeat_started", instance_id=INSTANCE_ID, interval=interval)
    while not shutdown.is_set():
        with session_scope() as session:
            beat(session, PROCESS_NAME, INSTANCE_ID)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("worker_heartbeat_stopping", instance_id=INSTANCE_ID)


async def _process_one_job(loop_id: int) -> bool:
    """Try to do one unit of work. Returns True if a job ran, False if
    the queue was empty or no account was available."""
    # Step 1 — claim. Capture a plain snapshot before the session closes.
    with session_scope() as session:
        job_orm = jobs.claim_next_job(session)
        snapshot = JobSnapshot.from_orm(job_orm) if job_orm is not None else None
    if snapshot is None:
        return False

    job_id = snapshot.id
    acquired: Optional[account_pool.AcquiredAccount] = None
    needs_account = scrapers.requires_account(snapshot.job_type)

    try:
        if needs_account:
            # Step 2 (instagrapi path) — acquire account. account_pool.acquire
            # reads `job.params` and `job.id`; we re-fetch the live ORM row
            # in this session so the access path is fully session-attached.
            with session_scope() as session:
                job_for_acquire = session.get(ScrapeJob, job_id)
                if job_for_acquire is None:
                    logger.warning("worker_job_disappeared", job_id=str(job_id))
                    return True
                try:
                    acquired = account_pool.acquire(session, job_for_acquire)
                except account_pool.NoAccountAvailable as exc:
                    jobs.mark_retry(session, job_id, str(exc), backoff_seconds=120)
                    logger.info("worker_no_account", loop_id=loop_id, job_id=str(job_id))
                    return True

            # Step 3 — bind account_id/proxy_id onto the row so the API
            # surface shows who picked it up.
            with session_scope() as session:
                row = session.get(ScrapeJob, job_id)
                if row is not None:
                    row.account_id = acquired.account.id
                    row.proxy_id = acquired.proxy.id if acquired.proxy else None
                    session.add(row)
        else:
            # HikerAPI / SaaS path — provider handles auth, no account
            # needed. We don't bind account_id; the row stays NULL on
            # those columns to mark "external scrape source".
            logger.info(
                "worker_account_skipped",
                loop_id=loop_id,
                job_id=str(job_id),
                job_type=snapshot.job_type,
                reason="scraper marked requires_account=False",
            )

        # Step 4 — run the scraper. Build a transient ORM instance from
        # the snapshot so existing scraper code (which expects a
        # ScrapeJob) keeps working without rewrites. This instance is
        # detached and immutable from the scraper's POV — fine, scrapers
        # only read.
        scraper_job = ScrapeJob(
            id=snapshot.id,
            job_type=snapshot.job_type,
            target=snapshot.target,
            params=snapshot.params,
            min_likes=snapshot.min_likes,
            min_impressions=snapshot.min_impressions,
            scan_target_id=snapshot.scan_target_id,
            attempt=snapshot.attempt,
            max_attempts=snapshot.max_attempts,
            status="running",
            scheduled_for=datetime.now(timezone.utc),
        )

        scraper_account = acquired.account if acquired else None
        scraper_proxy = acquired.proxy if acquired else None
        result = await scrapers.dispatch(scraper_job, scraper_account, scraper_proxy)

        # Step 5 — persist outcome + usage + release account, all
        # transactionally. Usage is per-account; if there's no account
        # (HikerAPI path) we skip incrementing since `ig_usage_daily`
        # is keyed on account_id.
        with session_scope() as session:
            if acquired is not None:
                usage.increment(
                    session,
                    acquired.account.id,
                    calls=result.api_calls,
                    posts=result.posts_saved,
                    comments=result.comments_saved,
                    stories=result.stories_saved,
                )
            if result.outcome == "success":
                jobs.mark_succeeded(session, job_id, result.stats)
                if acquired is not None:
                    account_pool.release(session, acquired, "success")
                if snapshot.scan_target_id is not None:
                    try:
                        from app.services import webhooks as webhooks_service

                        webhooks_service.enqueue_delivery(
                            session,
                            event_type="target_run_completed",
                            payload={
                                "target_id": str(snapshot.scan_target_id),
                                "job_id": str(job_id),
                                "job_type": snapshot.job_type,
                                "target": snapshot.target,
                                "stats": result.stats,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("worker_webhook_enqueue_failed")
            elif result.outcome in {"soft_fail", "rate_limited"}:
                jobs.mark_retry(session, job_id, result.error or result.outcome)
                if acquired is not None:
                    account_pool.release(session, acquired, result.outcome, detail=result.error)
            elif result.outcome == "challenge":
                jobs.mark_retry(session, job_id, result.error or "challenge required")
                if acquired is not None:
                    account_pool.release(session, acquired, "challenge", detail=result.error)
                    try:
                        from app.services import webhooks as webhooks_service

                        webhooks_service.enqueue_delivery(
                            session,
                            event_type="account_challenge_required",
                            payload={
                                "account_id": str(acquired.account.id),
                                "username": acquired.account.username,
                                "job_id": str(job_id),
                                "detail": result.error,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("worker_webhook_enqueue_failed")
            elif result.outcome == "fatal":
                jobs.mark_failed(session, job_id, result.error or "fatal")
                if acquired is not None:
                    account_pool.release(session, acquired, "fatal", detail=result.error)
            else:
                jobs.mark_failed(session, job_id, f"unknown outcome {result.outcome}")
                if acquired is not None:
                    account_pool.release(session, acquired, "fatal")

        return True

    except Exception as exc:  # noqa: BLE001
        # Anything escaping the scraper / pool is a soft fail with retry.
        logger.exception("worker_loop_exception", loop_id=loop_id, job_id=str(job_id))
        with session_scope() as session:
            jobs.mark_retry(session, job_id, f"{type(exc).__name__}: {exc}")
            if acquired is not None:
                account_pool.release(session, acquired, "soft_fail", detail=str(exc))
        return True


async def _worker_loop(loop_id: int, shutdown: asyncio.Event) -> None:
    """Per-loop driver. Sleeps between empty polls so we don't hammer
    the DB with SKIP LOCKED queries when the queue is idle."""
    logger.info("worker_loop_started", loop_id=loop_id, instance_id=INSTANCE_ID)
    idle_sleep = 2.0
    while not shutdown.is_set():
        try:
            did_work = await _process_one_job(loop_id)
        except Exception:  # noqa: BLE001
            # Outer safety net. _process_one_job should already swallow
            # all per-job exceptions; if one slips through, log it and
            # continue rather than crashing the whole worker.
            logger.exception("worker_loop_unhandled", loop_id=loop_id)
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=idle_sleep)
            except asyncio.TimeoutError:
                continue
    logger.info("worker_loop_stopping", loop_id=loop_id)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Catch SIGTERM/SIGINT and signal graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _trigger():
        if not shutdown.is_set():
            logger.info("worker_shutdown_signal", instance_id=INSTANCE_ID)
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

    concurrency = max(1, settings.IG_WORKER_CONCURRENCY)
    logger.info(
        "worker_starting",
        instance_id=INSTANCE_ID,
        concurrency=concurrency,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    tasks = [asyncio.create_task(_heartbeat_loop(shutdown), name="heartbeat")]
    for i in range(concurrency):
        tasks.append(asyncio.create_task(_worker_loop(i, shutdown), name=f"worker-{i}"))

    try:
        await shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("worker_stopped", instance_id=INSTANCE_ID)


if __name__ == "__main__":
    asyncio.run(main())
