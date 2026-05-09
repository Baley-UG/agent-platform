"""Scheduler entry point.

CP-M1 skeleton: the process boots, connects to Redis, and runs a slow
heartbeat tick. Real cron jobs (weekly plan auto-gen, plan filler,
publisher poller, stale-job alerter, webhook dispatcher) land in
CP-M6 and onward.
"""

from __future__ import annotations

import asyncio
import signal

from redis import Redis

from app.core.config import settings
from app.core.logging import logger


_shutdown = asyncio.Event()


async def _tick_loop() -> None:
    """Placeholder tick loop. Real schedulable work plugs in here later."""
    interval = settings.CP_SCHEDULER_TICK_SECONDS
    while not _shutdown.is_set():
        logger.info("content_pipeline_scheduler_tick", interval_s=interval)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def run() -> None:
    """Top-level scheduler coroutine."""
    redis_conn = Redis.from_url(settings.redis_url)
    try:
        redis_conn.ping()
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_redis_unreachable", error=str(exc))

    logger.info("content_pipeline_scheduler_starting", redis_db=settings.REDIS_DB)
    await _tick_loop()
    logger.info("content_pipeline_scheduler_stopped")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown.set)


def main() -> None:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        _install_signal_handlers(loop)
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
