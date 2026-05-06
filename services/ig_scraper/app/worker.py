"""Worker process entry point — placeholder for M3.

Will host the asyncio loop that claims jobs from ig_scrape_jobs via
SELECT ... FOR UPDATE SKIP LOCKED and runs them.

Run with: `python -m app.worker`.
"""

import asyncio

from app.core.logging import logger


async def main() -> None:
    logger.info("ig_scraper_worker_starting", note="placeholder, real logic lands in M3")
    # Keep the process alive without consuming resources so docker-compose
    # treats it as healthy until M3 replaces this with the real loop.
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
