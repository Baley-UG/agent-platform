"""Worker process — claims jobs and runs them.

Architecture:
- N concurrent worker tasks (`IG_WORKER_CONCURRENCY`), each running a
  `_worker_loop()`. Each task independently claims its own job via
  SKIP LOCKED, then claims its own account from the pool.
- One heartbeat task that touches `ig_worker_heartbeat` every
  `IG_HEARTBEAT_INTERVAL_SECONDS`.
- Graceful shutdown on SIGTERM / SIGINT — the main task sets a
  shutdown event; loops finish their current job before exiting.

Run with: `python -m app.worker`.
"""

import asyncio
import signal
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logging import logger
from app.services import account_pool, jobs, scrapers, usage
from app.services.database import session_scope
from app.services.heartbeat import beat, make_instance_id

PROCESS_NAME = "worker"
INSTANCE_ID = make_instance_id()


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
    # Claim a job. claim_next_job commits the running transition itself.
    with session_scope() as session:
        job = jobs.claim_next_job(session)
    if job is None:
        return False

    # Acquire an account for this job. If none available, push the job
    # back to queued with a short backoff so a different worker /
    # different time slot can pick it up.
    acquired: Optional[account_pool.AcquiredAccount] = None
    try:
        with session_scope() as session:
            try:
                acquired = account_pool.acquire(session, job)
            except account_pool.NoAccountAvailable as exc:
                jobs.mark_retry(session, job.id, str(exc), backoff_seconds=120)
                logger.info("worker_no_account", loop_id=loop_id, job_id=str(job.id))
                return True

        # Bind account to the job so /jobs/{id} shows who's running it.
        with session_scope() as session:
            row = session.get(type(job), job.id)
            if row is not None:
                row.account_id = acquired.account.id
                row.proxy_id = acquired.proxy.id if acquired.proxy else None
                session.add(row)

        result = await scrapers.dispatch(job, acquired.account, acquired.proxy)

        # Persist outcome + usage + release account, all transactionally.
        with session_scope() as session:
            usage.increment(
                session,
                acquired.account.id,
                calls=result.api_calls,
                posts=result.posts_saved,
                comments=result.comments_saved,
                stories=result.stories_saved,
            )
            if result.outcome == "success":
                jobs.mark_succeeded(session, job.id, result.stats)
                account_pool.release(session, acquired, "success")
            elif result.outcome in {"soft_fail", "rate_limited"}:
                jobs.mark_retry(session, job.id, result.error or result.outcome)
                account_pool.release(session, acquired, result.outcome, detail=result.error)
            elif result.outcome == "challenge":
                jobs.mark_retry(session, job.id, result.error or "challenge required")
                account_pool.release(session, acquired, "challenge", detail=result.error)
            elif result.outcome == "fatal":
                jobs.mark_failed(session, job.id, result.error or "fatal")
                account_pool.release(session, acquired, "fatal", detail=result.error)
            else:
                jobs.mark_failed(session, job.id, f"unknown outcome {result.outcome}")
                account_pool.release(session, acquired, "fatal")

        return True

    except Exception as exc:  # noqa: BLE001
        # Anything escaping the scraper / pool is a soft fail with retry.
        logger.exception("worker_loop_exception", loop_id=loop_id, job_id=str(job.id))
        with session_scope() as session:
            jobs.mark_retry(session, job.id, f"{type(exc).__name__}: {exc}")
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
