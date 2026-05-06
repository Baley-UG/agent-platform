"""Scraper dispatcher.

The worker loop calls `dispatch(job, account, proxy)` and gets back a
`ScrapeResult`. M3 ships a stub that just sleeps and reports zero work.
M4–M6 will register real scrapers per `job_type` here.

Registration model: each real scraper module imports
`register(job_type, fn)` and is imported eagerly from this `__init__`.
That way the dispatch table is built at import time and the worker
doesn't need to know about specific scrapers.
"""

import asyncio
from dataclasses import dataclass, field
from random import uniform
from typing import Awaitable, Callable, Dict, Optional

from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy


@dataclass
class ScrapeResult:
    """What a scraper returns to the worker.

    `outcome` drives `account_pool.release(...)` and
    `jobs.mark_*` decisions. Most successful scrapes return
    `outcome='success'` with stats; soft failures return
    `outcome='soft_fail'` with an error message and the worker decides
    whether to retry.
    """

    outcome: str = "success"  # success | soft_fail | challenge | rate_limited | fatal
    stats: Dict[str, int] = field(default_factory=dict)
    api_calls: int = 0
    posts_saved: int = 0
    comments_saved: int = 0
    stories_saved: int = 0
    error: Optional[str] = None


ScraperFn = Callable[[ScrapeJob, Account, Optional[Proxy]], Awaitable[ScrapeResult]]

_REGISTRY: Dict[str, ScraperFn] = {}


def register(job_type: str, fn: ScraperFn) -> None:
    """Register a scraper for `job_type`. M4–M6 modules call this at import."""
    _REGISTRY[job_type] = fn


async def _stub_scraper(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Placeholder used until M4 ships real scrapers.

    Sleeps 1–3s, returns a success result with zero stats. Useful for
    proving the queue / pool / heartbeat plumbing under load without
    making actual IG calls.
    """
    sleep_for = uniform(1.0, 3.0)
    logger.info(
        "stub_scraper_running",
        job_id=str(job.id),
        job_type=job.job_type,
        target=job.target,
        sleep_seconds=round(sleep_for, 2),
    )
    await asyncio.sleep(sleep_for)
    return ScrapeResult(outcome="success", api_calls=1, stats={"stub": 1})


async def dispatch(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Route a job to its registered scraper, or fall back to the stub."""
    fn = _REGISTRY.get(job.job_type, _stub_scraper)
    return await fn(job, account, proxy)
