"""Scraper dispatcher.

The worker loop calls `dispatch(job, account, proxy)` and gets back a
`ScrapeResult`. M3 ships a stub that just sleeps and reports zero work.
M4–M6 will register real scrapers per `job_type` here.

Registration model: each real scraper module imports
`register(job_type, fn)` and is imported eagerly from this `__init__`.
That way the dispatch table is built at import time and the worker
doesn't need to know about specific scrapers.

`requires_account=False` is the escape hatch for scrapers that talk to
external services (HikerAPI, Bright Data, Apify) that handle IG auth
themselves. Worker skips `account_pool.acquire()` for these.
"""

import asyncio
from dataclasses import dataclass, field
from random import uniform
from typing import Awaitable, Callable, Dict, Optional

from app.core.config import settings
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


ScraperFn = Callable[[ScrapeJob, Optional[Account], Optional[Proxy]], Awaitable[ScrapeResult]]


@dataclass
class ScraperRegistration:
    """Wraps the function with metadata the worker checks before dispatch."""

    fn: ScraperFn
    requires_account: bool = True


_REGISTRY: Dict[str, ScraperRegistration] = {}


def register(
    job_type: str,
    fn: ScraperFn,
    *,
    requires_account: bool = True,
) -> None:
    """Register a scraper for `job_type`.

    `requires_account=False` skips account_pool.acquire — used by
    HikerAPI / SaaS scrapers that handle IG auth on the provider side.
    """
    _REGISTRY[job_type] = ScraperRegistration(fn=fn, requires_account=requires_account)


def get_registration(job_type: str) -> Optional[ScraperRegistration]:
    """Worker uses this to decide whether to acquire an account."""
    return _REGISTRY.get(job_type)


def requires_account(job_type: str) -> bool:
    """Default True so unknown / stub job_types still go through pool."""
    reg = _REGISTRY.get(job_type)
    return reg.requires_account if reg is not None else True


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
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    """Route a job to its registered scraper, or fall back to the stub."""
    reg = _REGISTRY.get(job.job_type)
    if reg is None:
        return await _stub_scraper(job, account, proxy)
    return await reg.fn(job, account, proxy)


# ----------------------------------------------------------------------
# Eager registration of real scrapers.
# Imports live at the bottom to avoid circular-import problems at
# module load (user_feed imports from .persistence which imports from
# .features which imports from .simhash — none of those need scrapers).
# ----------------------------------------------------------------------

from app.services.scrapers.user_feed import (  # noqa: E402
    run_user_feed_full,
    run_user_feed_incremental,
)
from app.services.scrapers.user_stories import run_user_stories  # noqa: E402
from app.services.scrapers.user_highlights import run_user_highlights  # noqa: E402
from app.services.scrapers.hashtag import run_hashtag_recent, run_hashtag_top  # noqa: E402

register("user_feed_full", run_user_feed_full)
register("user_feed_incremental", run_user_feed_incremental)
register("user_stories", run_user_stories)
register("user_highlights", run_user_highlights)
register("hashtag_top", run_hashtag_top)
register("hashtag_recent", run_hashtag_recent)


# HikerAPI overrides — opt-in via env. When enabled, replaces the
# instagrapi-based scrapers above for every supported job_type.
if settings.USE_HIKERAPI:
    from app.services.scrapers.hikerapi import register_all as _register_hikerapi  # noqa: E402

    _register_hikerapi()
    logger.info("hikerapi_scrapers_registered", job_types=sorted(_REGISTRY.keys()))
