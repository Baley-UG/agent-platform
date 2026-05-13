"""HikerAPI port of user_stories.

HikerAPI offers `/v2/user/stories/by/username` (or by id). Returns the
currently-live tray. We persist via the same insert-only path as the
instagrapi version.
"""

from typing import Optional

from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy
from app.services.database import session_scope
from app.services.persistence import insert_story, upsert_ig_user
from app.services.scrapers import ScrapeResult
from app.services.scrapers.hikerapi.client import (
    HikerAPIClient,
    HikerAPIError,
    HikerAPINotFound,
    HikerAPIQuotaExceeded,
)


async def _fetch_user_id(client: HikerAPIClient, username: str) -> Optional[int]:
    from app.services.scrapers.hikerapi.user_feed import _unwrap

    try:
        raw = await client.get("/v2/user/by/username", username=username)
    except HikerAPINotFound:
        return None
    user = _unwrap(raw, ("pk", "id"))
    if user is None:
        logger.error("hikerapi_user_payload_unrecognised", username=username, sample=str(raw)[:500])
        return None
    pk_raw = user.get("pk") or user.get("id")
    try:
        return int(pk_raw)
    except (TypeError, ValueError):
        logger.error("hikerapi_user_pk_not_numeric", username=username, pk_value=pk_raw)
        return None


async def _fetch_stories(
    client: HikerAPIClient, *, user_id: int, username: Optional[str] = None
) -> list:
    """HikerAPI returns the full tray in one shot — no pagination needed.

    Endpoints (per openapi.json):
      - `/v2/user/stories/by/username?username=<u>`   → preferred when known
      - `/v2/user/stories?user_id=<id>`               → by-id fallback
      (note: spec is `/v2/user/stories`, NOT `/v2/user/stories/by/id`)

    Both responses share the shape:
      { "reel": { "items": [...story items...], ... }, "status": "ok" }
    We dig into `reel.items` to get the actual stories list.
    """
    if username:
        payload = await client.get("/v2/user/stories/by/username", username=username)
    else:
        payload = await client.get("/v2/user/stories", user_id=user_id)
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    # Canonical path: payload.reel.items (current HikerAPI shape).
    reel = payload.get("reel")
    if isinstance(reel, dict):
        items = reel.get("items")
        if isinstance(items, list):
            return items
    # Defensive fallbacks for older / future shapes.
    for key in ("stories", "items", "reels", "response"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return candidate
    return []


async def run_user_stories_hk(
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    """Capture currently-live stories via HikerAPI."""
    api_calls = 0
    try:
        async with HikerAPIClient() as client:
            user_id = await _fetch_user_id(client, job.target)
            api_calls += 1
            if user_id is None:
                return ScrapeResult(
                    outcome="fatal",
                    api_calls=api_calls,
                    error=f"User '{job.target}' not found on Instagram (HikerAPI 404).",
                )
            with session_scope() as session:
                upsert_ig_user(session, {"id": user_id, "username": job.target})

            stories = await _fetch_stories(client, user_id=user_id, username=job.target)
            api_calls += 1
    except HikerAPIQuotaExceeded as exc:
        return ScrapeResult(outcome="rate_limited", api_calls=api_calls, error=str(exc))
    except HikerAPIError as exc:
        return ScrapeResult(outcome="soft_fail", api_calls=api_calls, error=str(exc))

    saved = 0
    for story in stories:
        with session_scope() as session:
            if insert_story(session, story, author_id=user_id, job_id=job.id):
                saved += 1

    logger.info(
        "hikerapi_user_stories_completed",
        target=job.target,
        seen=len(stories),
        saved=saved,
    )
    return ScrapeResult(
        outcome="success",
        api_calls=api_calls,
        stories_saved=saved,
        stats={"stories_seen": len(stories), "stories_saved": saved, "source": "hikerapi"},
    )
