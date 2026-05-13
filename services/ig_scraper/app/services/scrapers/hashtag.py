"""Hashtag scrapers — `hashtag_top` and `hashtag_recent`.

Both reuse the same persistence path as user_feed (post + snapshot +
hashtags + audio). Author enrichment runs as a post-pass and may
auto-create new tracked targets — see `app/services/scrapers/enrichment.py`.

By default we don't fetch comments here: a hashtag scan can surface
hundreds of unrelated posts per run, and most of them aren't from
authors we care about. Comments are gated to the user_feed scrapers
where the per-author signal is strong.
"""

import asyncio
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
from app.services.scrapers.enrichment import enrich_authors
from app.services.scrapers.user_feed import (
    _build_authenticated_client,
    _classify_runtime_exception,
    _model_to_dict,
    _to_naive_utc,
)
from app.services.throttle import Throttle


async def _fetch_top(client, name: str, amount: int) -> List[Dict[str, Any]]:
    medias = await asyncio.to_thread(client.hashtag_medias_top_v1, name, amount)
    return [_model_to_dict(m) for m in medias]


async def _fetch_recent(client, name: str, amount: int) -> List[Dict[str, Any]]:
    medias = await asyncio.to_thread(client.hashtag_medias_recent_v1, name, amount)
    return [_model_to_dict(m) for m in medias]


def _normalise_hashtag(value: str) -> str:
    return value.lstrip("#").strip().lower()


async def _run(
    job: ScrapeJob,
    account: Account,
    proxy: Optional[Proxy],
    *,
    section: str,
) -> ScrapeResult:
    """Shared body. `section` ∈ {top, recent}."""
    name = _normalise_hashtag(job.target)
    if not name:
        return ScrapeResult(outcome="fatal", error="empty hashtag")

    params = job.params or {}
    max_posts = min(int(params.get("max_posts", 100)), settings.IG_MAX_POSTS_PER_JOB)
    auto_enrich = bool(params.get("auto_enrich_users", False))
    min_followers = int(
        params.get("min_followers_for_enrich", settings.IG_MIN_FOLLOWERS_FOR_ENRICH)
    )
    min_media = int(params.get("min_media_for_enrich", settings.IG_MIN_MEDIA_FOR_ENRICH))

    throttle = Throttle()

    try:
        client = _build_authenticated_client(account, proxy)
    except RuntimeError as exc:
        return ScrapeResult(outcome="fatal", error=str(exc))

    try:
        with session_scope() as session:
            upsert_hashtag(session, name)

        fetcher = _fetch_top if section == "top" else _fetch_recent
        medias = await fetcher(client, name, max_posts)
        await throttle.after_action("hashtag")
    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(
            outcome=_classify_runtime_exception(exc),
            error=f"{type(exc).__name__}: {exc}",
        )

    # Filter parsing — hashtag jobs reuse min_likes / min_impressions /
    # since just like user_feed jobs.
    since: Optional[datetime] = None
    if params.get("since"):
        try:
            since = _to_naive_utc(datetime.fromisoformat(params["since"]))
        except (TypeError, ValueError):
            since = None

    posts_saved = 0
    skipped = 0
    seen_authors: Dict[int, Dict[str, Any]] = {}

    for media in medias:
        author_payload = media.get("user") or {}
        author_id = int(author_payload.get("pk") or author_payload.get("id") or 0)
        if not author_id:
            skipped += 1
            continue

        # Capture author for the enrichment pass — favour the richer
        # payload if we see the same author multiple times in one batch.
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

    enrichment_stats = {"users_enriched": 0, "targets_created": 0}
    if auto_enrich and seen_authors:
        try:
            enrichment_stats = await enrich_authors(
                client,
                throttle,
                authors=seen_authors,
                source_target_id=job.scan_target_id,
                min_followers=min_followers,
                min_media=min_media,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("enrichment_pass_failed", hashtag=name, error=str(exc))

    logger.info(
        "hashtag_scrape_completed",
        section=section,
        hashtag=name,
        seen=len(medias),
        saved=posts_saved,
        skipped=skipped,
        unique_authors=len(seen_authors),
        users_enriched=enrichment_stats["users_enriched"],
        targets_created=enrichment_stats["targets_created"],
    )

    return ScrapeResult(
        outcome="success",
        api_calls=1 + enrichment_stats["users_enriched"],
        posts_saved=posts_saved,
        stats={
            "posts_seen": len(medias),
            "posts_saved": posts_saved,
            "skipped_by_filter": skipped,
            "unique_authors": len(seen_authors),
            "users_enriched": enrichment_stats["users_enriched"],
            "targets_created": enrichment_stats["targets_created"],
        },
    )


async def run_hashtag_top(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Top section — IG's curated 'best' posts under the hashtag."""
    return await _run(job, account, proxy, section="top")


async def run_hashtag_recent(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Recent section — chronological feed under the hashtag."""
    return await _run(job, account, proxy, section="recent")
