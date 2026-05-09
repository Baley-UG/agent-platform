"""Scheduler process — turns due tracked targets into queued jobs.

One process, single tick loop. The tick body uses
`SELECT ... FOR UPDATE SKIP LOCKED` so even if two scheduler replicas
were started by mistake, they won't double-enqueue. In practice there
should be exactly one scheduler replica.

Heartbeat under `process='scheduler'` so `/ready` knows it's alive.
"""

import asyncio
import signal
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger
from app.services import (
    jobs as jobs_service,
    retention,
    scoring,
    targets as targets_service,
    webhooks as webhooks_service,
)
from app.services.database import session_scope
from app.services.heartbeat import beat, make_instance_id

# We run the heavy nightly recompute / view refresh once per UTC day,
# at a quiet hour. This is the canonical "low traffic on Instagram"
# slot and gives the daily fleet a clean state for the morning.
_DAILY_HOUR_UTC = 3

PROCESS_NAME = "scheduler"
INSTANCE_ID = make_instance_id("sched")


async def _heartbeat_loop(shutdown: asyncio.Event) -> None:
    """Touch the scheduler heartbeat row every IG_HEARTBEAT_INTERVAL_SECONDS."""
    interval = settings.IG_HEARTBEAT_INTERVAL_SECONDS
    logger.info(
        "scheduler_heartbeat_started",
        instance_id=INSTANCE_ID,
        interval=interval,
    )
    while not shutdown.is_set():
        with session_scope() as session:
            beat(session, PROCESS_NAME, INSTANCE_ID)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler_heartbeat_stopping", instance_id=INSTANCE_ID)


async def _tick_loop(shutdown: asyncio.Event) -> None:
    """Main scheduler body — fires once per IG_SCHEDULER_TICK_SECONDS."""
    interval = settings.IG_SCHEDULER_TICK_SECONDS
    logger.info(
        "scheduler_tick_started",
        instance_id=INSTANCE_ID,
        tick_seconds=interval,
    )
    while not shutdown.is_set():
        try:
            with session_scope() as session:
                jobs_created = targets_service.enqueue_jobs_for_due_targets(session)
            if jobs_created:
                logger.info("scheduler_tick", jobs_created=jobs_created)
        except Exception:  # noqa: BLE001
            # A bad row or transient DB blip mustn't crash the loop.
            logger.exception("scheduler_tick_failed")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler_tick_stopping", instance_id=INSTANCE_ID)


async def _daily_loop(shutdown: asyncio.Event) -> None:
    """Run score recompute + view refresh + retention + warmup at 03:00 UTC.

    State (last-run timestamp) lives in memory; restarting the
    scheduler means the next-day check fires once-extra at startup
    if we missed the window. That's idempotent and cheap.
    """
    last_run_date: Optional[datetime] = None
    logger.info("scheduler_daily_loop_started", hour_utc=_DAILY_HOUR_UTC)

    while not shutdown.is_set():
        now = datetime.now(timezone.utc)
        ready = now.hour >= _DAILY_HOUR_UTC and (
            last_run_date is None or last_run_date.date() < now.date()
        )
        if ready:
            try:
                with session_scope() as session:
                    scored = scoring.recompute_recent_batch(session)
                refreshed = []
                with session_scope() as session:
                    refreshed = scoring.refresh_views(session, concurrently=True)
                # GDPR retention pass — no-op when IG_RETENTION_ENABLED=false.
                ttl_applied = nullified = {"comments": 0, "biographies": 0}
                with session_scope() as session:
                    ttl_applied = retention.apply_default_ttl(session)
                with session_scope() as session:
                    nullified = retention.nullify_expired(session)
                # M10 — auto-promote `fresh` accounts past warmup.
                promoted = _promote_warmed_up_accounts()

                logger.info(
                    "scheduler_daily_completed",
                    scored_posts=scored,
                    refreshed_views=refreshed,
                    ttl_applied=ttl_applied,
                    nullified=nullified,
                    promoted=promoted,
                )
                last_run_date = now
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_daily_failed")

        # Sleep until the next probable window (~5 minutes is fine —
        # we never need sub-minute resolution for daily work).
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=300)
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler_daily_loop_stopping")


async def _reaper_loop(shutdown: asyncio.Event) -> None:
    """Reap jobs whose worker died mid-process.

    Runs every IG_REAPER_INTERVAL_SECONDS. Resets `status='running'`
    rows whose `started_at` is older than IG_JOB_STUCK_AFTER_MINUTES
    so another worker picks them up. Bounded by `max_attempts`, so a
    job that genuinely can't be processed will hit terminal `failed`.
    """
    interval = settings.IG_REAPER_INTERVAL_SECONDS
    minutes = settings.IG_JOB_STUCK_AFTER_MINUTES
    logger.info(
        "scheduler_reaper_loop_started",
        interval=interval,
        stuck_after_minutes=minutes,
    )
    # First pass at startup so a fresh scheduler cleans up after a
    # crashed previous instance immediately.
    while not shutdown.is_set():
        try:
            with session_scope() as session:
                jobs_service.reap_stuck_jobs(session, older_than_minutes=minutes)
        except Exception:  # noqa: BLE001
            logger.exception("reaper_loop_failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler_reaper_loop_stopping")


async def _webhook_dispatch_loop(shutdown: asyncio.Event) -> None:
    """Fire pending webhook deliveries every IG_WEBHOOK_DISPATCH_INTERVAL_SECONDS."""
    interval = settings.IG_WEBHOOK_DISPATCH_INTERVAL_SECONDS
    logger.info("scheduler_webhook_loop_started", interval=interval)
    while not shutdown.is_set():
        try:
            await webhooks_service.fire_pending_deliveries(
                batch=settings.IG_WEBHOOK_BATCH_SIZE
            )
        except Exception:  # noqa: BLE001
            logger.exception("webhook_dispatch_loop_failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
    logger.info("scheduler_webhook_loop_stopping")


async def _canary_loop(shutdown: asyncio.Event) -> None:
    """Hourly probe job for IG_CANARY_TARGET, when configured.

    The job is a small `user_feed_incremental` flagged with
    `params.canary=True` so the account_pool routes it to a
    `role='canary'` account. Its only purpose is to detect instagrapi
    breakage early — if it starts failing, ops know the rest of the
    fleet is about to follow.
    """
    if not settings.IG_CANARY_TARGET:
        logger.info("scheduler_canary_disabled")
        return

    interval_seconds = settings.IG_CANARY_INTERVAL_HOURS * 3600
    last_run: Optional[datetime] = None
    logger.info(
        "scheduler_canary_loop_started",
        target=settings.IG_CANARY_TARGET,
        interval_hours=settings.IG_CANARY_INTERVAL_HOURS,
    )

    while not shutdown.is_set():
        now = datetime.now(timezone.utc)
        if last_run is None or (now - last_run).total_seconds() >= interval_seconds:
            try:
                with session_scope() as session:
                    session.execute(
                        text(
                            """
                            INSERT INTO ig_scrape_jobs
                                (id, job_type, target, status, priority, params,
                                 max_attempts, scheduled_for, created_at)
                            VALUES (
                                gen_random_uuid(), 'user_feed_incremental',
                                :target, 'queued', 50,
                                CAST(:params AS jsonb), 1, :now, :now
                            )
                            """
                        ),
                        {
                            "target": settings.IG_CANARY_TARGET,
                            "params": '{"canary": true, "max_posts": 5, "fetch_comments": false}',
                            "now": now,
                        },
                    )
                logger.info("scheduler_canary_enqueued", target=settings.IG_CANARY_TARGET)
                last_run = now
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_canary_failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=300)
        except asyncio.TimeoutError:
            continue


def _promote_warmed_up_accounts() -> int:
    """Flip `fresh` accounts older than IG_WARMUP_HOURS to `mid`.

    Returns the count promoted. Cheap single UPDATE ... RETURNING.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.IG_WARMUP_HOURS)
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                UPDATE ig_accounts
                SET quota_tier = 'mid', updated_at = now()
                WHERE quota_tier = 'fresh'
                  AND COALESCE(onboarded_at, created_at) <= :cutoff
                RETURNING id
                """
            ),
            {"cutoff": cutoff},
        ).all()
        if rows:
            logger.info("warmup_promoted_accounts", count=len(rows))
        return len(rows)


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _trigger():
        if not shutdown.is_set():
            logger.info("scheduler_shutdown_signal", instance_id=INSTANCE_ID)
            shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _trigger)
        except NotImplementedError:
            pass


async def main() -> None:
    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    logger.info("scheduler_starting", instance_id=INSTANCE_ID)
    tasks = [
        asyncio.create_task(_heartbeat_loop(shutdown), name="scheduler-heartbeat"),
        asyncio.create_task(_tick_loop(shutdown), name="scheduler-tick"),
        asyncio.create_task(_reaper_loop(shutdown), name="scheduler-reaper"),
        asyncio.create_task(_daily_loop(shutdown), name="scheduler-daily"),
        asyncio.create_task(_webhook_dispatch_loop(shutdown), name="scheduler-webhooks"),
        asyncio.create_task(_canary_loop(shutdown), name="scheduler-canary"),
    ]
    try:
        await shutdown.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("scheduler_stopped", instance_id=INSTANCE_ID)


if __name__ == "__main__":
    asyncio.run(main())
