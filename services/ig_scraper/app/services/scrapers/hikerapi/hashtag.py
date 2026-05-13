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


def _extract_medias_from_sections(response: dict) -> list:
    """Walk Instagram-style `response.sections[].layout_content.medias[].media`.

    HikerAPI mirrors Instagram's hashtag GraphQL output verbatim, which
    is a list of layout sections rather than a flat media array. Each
    section may carry medias in different sub-fields depending on the
    layout type (`media_grid` uses `layout_content.medias`, others may
    use `layout_content.one_by_two_item.clips.items`, etc.). We
    enumerate the known nesting paths and yield the inner `media`
    dicts.
    """
    out: list = []
    sections = response.get("sections") if isinstance(response, dict) else None
    if not isinstance(sections, list):
        return out
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        content = sec.get("layout_content") or {}
        if not isinstance(content, dict):
            continue
        # Path 1: layout_content.medias[].media  (most common — `media_grid`)
        medias = content.get("medias")
        if isinstance(medias, list):
            for wrap in medias:
                if isinstance(wrap, dict):
                    m = wrap.get("media") if "media" in wrap else wrap
                    if isinstance(m, dict):
                        out.append(m)
        # Path 2: layout_content.one_by_two_item.clips.items[].media
        clips_block = content.get("one_by_two_item", {}).get("clips", {}).get("items")
        if isinstance(clips_block, list):
            for wrap in clips_block:
                if isinstance(wrap, dict):
                    m = wrap.get("media") if "media" in wrap else wrap
                    if isinstance(m, dict):
                        out.append(m)
        # Path 3: layout_content.fill_items[].media (occasional grid filler)
        fillers = content.get("fill_items")
        if isinstance(fillers, list):
            for wrap in fillers:
                if isinstance(wrap, dict):
                    m = wrap.get("media") if "media" in wrap else wrap
                    if isinstance(m, dict):
                        out.append(m)
    return out


async def _iter_hashtag_medias(
    client: HikerAPIClient,
    *,
    section: str,
    name: str,
    max_items: int,
):
    """Yield each normalised hashtag media as it arrives.

    Streaming iterator so a mid-flight 402/429 leaves everything-so-far
    persisted by the caller. Pagination uses the `page_id` query param
    (NOT `end_cursor`, which is what paginate_chunks defaults to), and
    the cursor in the response is `next_page_id`. The items themselves
    are not a flat list — they live in
    `response.sections[].layout_content.medias[].media`, so we extract
    them via `_extract_medias_from_sections`.

    Both `top` and `recent` sections are real HikerAPI endpoints. Unknown
    section names fall back to `top`.
    """
    if section not in ("top", "recent"):
        logger.warning(
            "hikerapi_hashtag_unknown_section",
            section=section,
            name=name,
            note="expected 'top' or 'recent' — falling back to top.",
        )
        section = "top"
    path = f"/v2/hashtag/medias/{section}"

    page_id: Optional[str] = None
    yielded = 0
    while True:
        params = {"name": name}
        if page_id:
            params["page_id"] = page_id
        page = await client.get(path, **params)
        if not isinstance(page, dict):
            return
        response = page.get("response") or {}
        medias = _extract_medias_from_sections(response)
        for media in medias:
            yield _normalise_media(media)
            yielded += 1
            if yielded >= max_items:
                return
        page_id = page.get("next_page_id")
        # `more_available` can confirm we're at the end; absence of
        # next_page_id is the harder stop signal.
        if not page_id:
            return
        if not response.get("more_available", True):
            return


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
    seen_count = 0
    seen_authors: Dict[int, Dict[str, Any]] = {}
    partial_failure_exc: Optional[HikerAPIError] = None

    try:
        async with HikerAPIClient() as client:
            with session_scope() as session:
                upsert_hashtag(session, name)

            try:
                async for media in _iter_hashtag_medias(
                    client, section=section, name=name, max_items=max_posts
                ):
                    seen_count += 1
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
            except HikerAPIError as exc:
                partial_failure_exc = exc
                logger.warning(
                    "hikerapi_hashtag_pagination_interrupted",
                    error=str(exc),
                    posts_saved=posts_saved,
                    posts_seen=seen_count,
                )
    except HikerAPIQuotaExceeded as exc:
        return ScrapeResult(outcome="rate_limited", api_calls=api_calls, error=str(exc))
    except HikerAPIError as exc:
        return ScrapeResult(outcome="soft_fail", api_calls=api_calls, error=str(exc))

    api_calls += max(1, (seen_count // settings.HIKERAPI_PAGE_SIZE) + 1)

    if partial_failure_exc is not None:
        outcome = (
            "rate_limited"
            if isinstance(partial_failure_exc, HikerAPIQuotaExceeded)
            else "soft_fail"
        )
        return ScrapeResult(
            outcome=outcome,
            api_calls=api_calls,
            posts_saved=posts_saved,
            error=f"{type(partial_failure_exc).__name__}: {partial_failure_exc}",
            stats={
                "posts_seen": seen_count,
                "posts_saved": posts_saved,
                "skipped_by_filter": skipped,
                "unique_authors": len(seen_authors),
                "source": "hikerapi",
                "partial": True,
            },
        )

    logger.info(
        "hikerapi_hashtag_completed",
        section=section,
        hashtag=name,
        seen=seen_count,
        saved=posts_saved,
        skipped=skipped,
        unique_authors=len(seen_authors),
    )
    return ScrapeResult(
        outcome="success",
        api_calls=api_calls,
        posts_saved=posts_saved,
        stats={
            "posts_seen": seen_count,
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
