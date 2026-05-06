"""Story scraper — captures the currently-live story tray for a user.

Stories disappear after 24h on Instagram, so this job MUST run at least
daily for any target with `fetch_stories=true`. Missing a day = the
content is gone for good (unless the owner saved it to a highlight).

The scraper is intentionally small — no filter, no comments, no
pagination (instagrapi returns the full tray in one call).
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy
from app.services.database import session_scope
from app.services.persistence import insert_story, upsert_ig_user
from app.services.scrapers import ScrapeResult
from app.services.scrapers.user_feed import (
    _build_authenticated_client,
    _classify_runtime_exception,
    _model_to_dict,
)
from app.services.throttle import Throttle


async def _fetch_user_id(client, username: str) -> int:
    return await asyncio.to_thread(client.user_id_from_username_v1, username)


async def _fetch_stories(client, user_id: int) -> List[Dict[str, Any]]:
    stories = await asyncio.to_thread(client.user_stories_v1, user_id)
    return [_model_to_dict(s) for s in stories]


async def run_user_stories(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Capture currently-live stories for `job.target` (a username)."""
    throttle = Throttle()

    try:
        client = _build_authenticated_client(account, proxy)
    except RuntimeError as exc:
        return ScrapeResult(outcome="fatal", error=str(exc))

    try:
        user_id = await _fetch_user_id(client, job.target)
        await throttle.after_action("profile")

        stories = await _fetch_stories(client, user_id)
        await throttle.after_action("story")
    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(
            outcome=_classify_runtime_exception(exc),
            error=f"{type(exc).__name__}: {exc}",
        )

    saved = 0
    for story in stories:
        with session_scope() as session:
            # Make sure the author row exists (FK target).
            upsert_ig_user(
                session,
                {"id": user_id, "username": job.target},
            )
            if insert_story(session, story, author_id=user_id, job_id=job.id):
                saved += 1

    logger.info(
        "user_stories_completed",
        target=job.target,
        seen=len(stories),
        saved=saved,
    )
    return ScrapeResult(
        outcome="success",
        api_calls=2,  # user_id_from_username + user_stories
        stories_saved=saved,
        stats={"stories_seen": len(stories), "stories_saved": saved},
    )
