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

from app.core.config import settings
from app.core.logging import logger
from app.services import scoring, targets as targets_service
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
    """Run score recompute + view refresh once per UTC day.

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
                logger.info(
                    "scheduler_daily_completed",
                    scored_posts=scored,
                    refreshed_views=refreshed,
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
        asyncio.create_task(_daily_loop(shutdown), name="scheduler-daily"),
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
