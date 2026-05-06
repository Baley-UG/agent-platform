"""Highlight scraper — opt-in deep capture of saved story containers.

Unlike `user_stories`, highlights stay visible indefinitely. Default
behaviour is to scan once when a target is first added; the scheduler
can opt back in via `params.fetch_highlights=true`.

Walk:
    user_highlights(user_id) → list of highlights (lightweight)
    for each highlight: highlight_info(highlight_pk) → list of story-like
        items, persisted via the stories pipeline + linked back to the
        highlight via ig_highlight_items.
"""

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy
from app.services.database import session_scope
from app.services.persistence import (
    insert_story,
    link_highlight_item,
    upsert_highlight,
    upsert_ig_user,
)
from app.services.scrapers import ScrapeResult
from app.services.scrapers.user_feed import (
    _build_authenticated_client,
    _classify_runtime_exception,
    _model_to_dict,
)
from app.services.throttle import Throttle


async def _fetch_user_id(client, username: str) -> int:
    return await asyncio.to_thread(client.user_id_from_username_v1, username)


async def _fetch_highlights(client, user_id: int) -> List[Dict[str, Any]]:
    highlights = await asyncio.to_thread(client.user_highlights, user_id)
    return [_model_to_dict(h) for h in highlights]


async def _fetch_highlight_info(client, highlight_pk: int) -> Dict[str, Any]:
    info = await asyncio.to_thread(client.highlight_info, highlight_pk)
    return _model_to_dict(info)


async def run_user_highlights(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Persist all of `job.target`'s highlight containers and items."""
    throttle = Throttle()

    try:
        client = _build_authenticated_client(account, proxy)
    except RuntimeError as exc:
        return ScrapeResult(outcome="fatal", error=str(exc))

    try:
        user_id = await _fetch_user_id(client, job.target)
        await throttle.after_action("profile")
        with session_scope() as session:
            upsert_ig_user(session, {"id": user_id, "username": job.target})

        highlights = await _fetch_highlights(client, user_id)
        await throttle.after_action("profile")

        items_total = 0
        for highlight in highlights:
            highlight_pk = int(highlight.get("pk") or highlight.get("id") or 0)
            if not highlight_pk:
                continue
            try:
                info = await _fetch_highlight_info(client, highlight_pk)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "highlight_info_failed",
                    highlight_pk=highlight_pk,
                    error=str(exc),
                )
                continue
            await throttle.after_action("story")

            with session_scope() as session:
                upsert_highlight(session, info, owner_id=user_id)
                items = info.get("items") or []
                for index, item in enumerate(items):
                    story_pk = int(item.get("pk") or item.get("id") or 0)
                    if not story_pk:
                        continue
                    insert_story(session, item, author_id=user_id, job_id=job.id)
                    link_highlight_item(session, highlight_pk, story_pk, position=index)
                    items_total += 1

    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(
            outcome=_classify_runtime_exception(exc),
            error=f"{type(exc).__name__}: {exc}",
        )

    logger.info(
        "user_highlights_completed",
        target=job.target,
        highlights=len(highlights),
        items=items_total,
    )
    return ScrapeResult(
        outcome="success",
        api_calls=2 + len(highlights),
        stories_saved=items_total,
        stats={
            "highlights_seen": len(highlights),
            "highlight_items_saved": items_total,
        },
    )
