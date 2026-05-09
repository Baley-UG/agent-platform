"""HikerAPI port of hashtag_top / hashtag_recent.

Endpoints:
  GET /v2/hashtag/medias/top/chunk?name=<tag>&end_cursor=<c>
  GET /v2/hashtag/medias/recent/chunk?name=<tag>&end_cursor=<c>

Persists posts via the existing pipeline. Author enrichment subroutine
is skipped here — it lives in the instagrapi flow because it needs
session-aware `user_info_v1` calls. With HikerAPI we have a simpler
option: every author payload returned alongside hashtag medias is
already enriched with profile fields, so we upsert it directly.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy
from app.services.database import session_scope
from app.services.filters import passes_filter
from app.services.persistence import (
    upsert_audio_track,
    upsert_hashtag,
    upsert_ig_user,
    upsert_post,
)
from app.services.scrapers import ScrapeResult
from app.core.logging import logger as _hashtag_logger  # noqa: F401
from app.services.scrapers.hikerapi.client import (
    HikerAPIClient,
    HikerAPIError,
    HikerAPIQuotaExceeded,
)
from app.services.scrapers.hikerapi.user_feed import (
    _normalise_media,
    _to_naive_utc,
)


def _normalise_hashtag(value: str) -> str:
    return value.lstrip("#").strip().lower()


async def _walk_hashtag_medias(
    client: HikerAPIClient,
    *,
    section: str,
    name: str,
    max_items: int,
) -> List[Dict[str, Any]]:
    """Fetch paginated hashtag medias.

    HikerAPI v2 only exposes `/v2/hashtag/medias/top` — no `/recent`
    sibling, no `/chunk` suffix. The endpoint itself returns a
    paginated payload; `paginate_chunks` walks `end_cursor` if it's
    present, otherwise stops after the first page.

    `section` is kept for caller compatibility but only `top` is
    actually supported by HikerAPI; `recent` falls back to `top` with
    a warning.
    """
    if section != "top":
        logger.warning(
            "hikerapi_hashtag_recent_unsupported",
            section=section,
            name=name,
            note="HikerAPI v2 only exposes /v2/hashtag/medias/top; falling back to top.",
        )
    path = "/v2/hashtag/medias/top"
    medias: List[Dict[str, Any]] = []
    async for media in client.paginate_chunks(
        path,
        items_key="response",
        max_items=max_items,
        name=name,
    ):
        medias.append(_normalise_media(media))
    return medias


async def _run(
    job: ScrapeJob,
    *,
    section: str,
) -> ScrapeResult:
    name = _normalise_hashtag(job.target)
    if not name:
        return ScrapeResult(outcome="fatal", error="empty hashtag")

    params = job.params or {}
    max_posts = min(int(params.get("max_posts", 100)), settings.IG_MAX_POSTS_PER_JOB)

    since: Optional[datetime] = None
    if params.get("since"):
        try:
            since = _to_naive_utc(datetime.fromisoformat(params["since"]))
        except (TypeError, ValueError):
            since = None

    api_calls = 0
    posts_saved = 0
    skipped = 0
    seen_authors: Dict[int, Dict[str, Any]] = {}

    try:
        async with HikerAPIClient() as client:
            with session_scope() as session:
                upsert_hashtag(session, name)

            medias = await _walk_hashtag_medias(
                client, section=section, name=name, max_items=max_posts
            )
            api_calls += max(1, (len(medias) // settings.HIKERAPI_PAGE_SIZE) + 1)
    except HikerAPIQuotaExceeded as exc:
        return ScrapeResult(outcome="rate_limited", api_calls=api_calls, error=str(exc))
    except HikerAPIError as exc:
        return ScrapeResult(outcome="soft_fail", api_calls=api_calls, error=str(exc))

    for media in medias:
        author_payload = media.get("user") or {}
        author_id = int(author_payload.get("pk") or author_payload.get("id") or 0)
        if not author_id:
            skipped += 1
            continue

        existing = seen_authors.get(author_id)
        if existing is None or len(author_payload) > len(existing):
            seen_authors[author_id] = author_payload

        decision = passes_filter(
            like_count=int(media.get("like_count") or 0),
            play_count=media.get("play_count"),
            view_count=media.get("view_count"),
            taken_at=_to_naive_utc(media.get("taken_at")) or datetime.now(timezone.utc),
            min_likes=job.min_likes,
            min_impressions=job.min_impressions,
            since=since,
        )
        if not decision.passed:
            skipped += 1
            continue

        with session_scope() as session:
            upsert_ig_user(session, {"id": author_id, **author_payload})
            audio_id = upsert_audio_track(session, media.get("music_info"))
            upsert_post(
                session,
                media=media,
                author_id=author_id,
                job_id=job.id,
                audio_track_id=audio_id,
            )
            posts_saved += 1

    logger.info(
        "hikerapi_hashtag_completed",
        section=section,
        hashtag=name,
        seen=len(medias),
        saved=posts_saved,
        skipped=skipped,
        unique_authors=len(seen_authors),
    )
    return ScrapeResult(
        outcome="success",
        api_calls=api_calls,
        posts_saved=posts_saved,
        stats={
            "posts_seen": len(medias),
            "posts_saved": posts_saved,
            "skipped_by_filter": skipped,
            "unique_authors": len(seen_authors),
            "source": "hikerapi",
        },
    )


async def run_hashtag_top_hk(
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    return await _run(job, section="top")


async def run_hashtag_recent_hk(
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    return await _run(job, section="recent")
