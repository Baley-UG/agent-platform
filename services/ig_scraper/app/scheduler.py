"""Scheduler process entry point — placeholder for M7.

Will tick every IG_SCHEDULER_TICK_SECONDS, find due ig_scan_targets,
and turn them into queued ig_scrape_jobs.

Run with: `python -m app.scheduler`.
"""

import asyncio

from app.core.config import settings
from app.core.logging import logger


async def main() -> None:
    logger.info(
        "ig_scraper_scheduler_starting",
        tick_seconds=settings.IG_SCHEDULER_TICK_SECONDS,
        note="placeholder, real logic lands in M7",
    )
    while True:
        await asyncio.sleep(settings.IG_SCHEDULER_TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
