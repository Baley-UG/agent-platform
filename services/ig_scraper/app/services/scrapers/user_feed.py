"""Username feed scrapers — `user_feed_full` and `user_feed_incremental`.

Both hit the same instagrapi endpoints; they differ in their stop
condition and post-run cursor management:

- `user_feed_full` walks the full feed AND `user_clips_paginated`,
  merging by media pk. Used the first time we touch a target.
- `user_feed_incremental` walks the same endpoints but stops at the
  cursor stored on `ig_scan_targets` (last_seen_post_id /
  last_seen_taken_at). Comments are only fetched for newly-seen posts
  on incremental runs.

instagrapi is sync; we wrap each remote call with `asyncio.to_thread`
so the event loop stays responsive. Anti-detection delays come from
`Throttle.after_action(...)`.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.core.config import settings
from app.core.logging import logger
from app.models.account import Account
from app.models.job import ScrapeJob
from app.models.proxy import Proxy
from app.services.database import session_scope
from app.services.filters import passes_filter
from app.services.instagrapi_client import build_proxy_url
from app.services.persistence import (
    upsert_audio_track,
    upsert_comments,
    upsert_ig_user,
    upsert_post,
)
from app.services.scrapers import ScrapeResult
from app.services.throttle import Throttle


def _import_client():
    """Lazy import: instagrapi pulls in PIL / moviepy and ~5s of import
    time. Tests can monkey-patch this to inject a fake."""
    from instagrapi import Client  # type: ignore

    return Client


def _build_authenticated_client(account: Account, proxy: Optional[Proxy]):
    """Reload an existing instagrapi session for `account`.

    Assumes M2's login already populated `account.session_blob`. If the
    session has been wiped (or the account row was created without
    login), the worker should never have picked it; we defensively
    raise here anyway.
    """
    if not account.session_blob:
        raise RuntimeError(
            f"account {account.username} has no session_blob — run /accounts/{account.id}/login first"
        )

    Client = _import_client()
    client = Client()
    client.set_settings(account.session_blob)
    proxy_url = build_proxy_url(proxy)
    if proxy_url:
        client.set_proxy(proxy_url)
    return client


def _model_to_dict(obj: Any) -> Dict[str, Any]:
    """instagrapi pydantic models → dicts. Falls back to vars()."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if hasattr(obj, "dict"):
        return obj.dict()
    return dict(vars(obj))


def _to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ----------------------------------------------------------------------
# Pagination wrappers
# ----------------------------------------------------------------------


async def _fetch_user_id(client, username: str) -> int:
    return await asyncio.to_thread(client.user_id_from_username_v1, username)


async def _fetch_user_info(client, user_id: int) -> Dict[str, Any]:
    info = await asyncio.to_thread(client.user_info_v1, user_id)
    return _model_to_dict(info)


async def _fetch_feed_page(client, user_id: int, page_size: int, end_cursor: Optional[str]):
    """One feed page. instagrapi's paginated helper returns (items, next_cursor)."""
    medias, next_cursor = await asyncio.to_thread(
        client.user_medias_paginated_v1, user_id, page_size, end_cursor or ""
    )
    return [_model_to_dict(m) for m in medias], next_cursor


async def _fetch_clips_page(client, user_id: int, page_size: int, end_cursor: Optional[str]):
    """One reels-only page; some accounts post reels that don't surface in user_medias."""
    try:
        medias, next_cursor = await asyncio.to_thread(
            client.user_clips_paginated_v1, user_id, page_size, end_cursor or ""
        )
    except AttributeError:
        # Older instagrapi versions don't expose this helper.
        return [], None
    return [_model_to_dict(m) for m in medias], next_cursor


async def _fetch_comments(client, media_pk: int, amount: int) -> List[Dict[str, Any]]:
    comments = await asyncio.to_thread(client.media_comments, media_pk, amount)
    return [_model_to_dict(c) for c in comments]


# ----------------------------------------------------------------------
# Feed walker
# ----------------------------------------------------------------------


async def _walk_feed(
    client,
    user_id: int,
    *,
    fetcher,
    stop_at_post_id: Optional[int],
    stop_at_taken_at: Optional[datetime],
    hard_cap: int,
    throttle: Throttle,
) -> List[Dict[str, Any]]:
    """Generic walker for paginated feed/clip endpoints.

    Stops on either the cursor (incremental) or `hard_cap` (full backfill).
    """
    collected: List[Dict[str, Any]] = []
    end_cursor: Optional[str] = None
    page_size = 50

    while True:
        page, end_cursor = await fetcher(client, user_id, page_size, end_cursor)
        await throttle.after_action("feed")
        if not page:
            break

        hit_cursor = False
        for media in page:
            media_id = int(media.get("pk") or media.get("id") or 0)
            taken_at = _to_naive_utc(media.get("taken_at"))
            if stop_at_post_id and media_id == stop_at_post_id:
                hit_cursor = True
                break
            if stop_at_taken_at and taken_at and taken_at <= stop_at_taken_at:
                hit_cursor = True
                break
            collected.append(media)
            if len(collected) >= hard_cap:
                hit_cursor = True
                break

        if hit_cursor or not end_cursor:
            break

    return collected


def _merge_dedupe(*lists: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge multiple media lists, preferring the first occurrence per pk."""
    seen: set[int] = set()
    out: List[Dict[str, Any]] = []
    for lst in lists:
        for media in lst:
            pk = int(media.get("pk") or media.get("id") or 0)
            if pk and pk not in seen:
                seen.add(pk)
                out.append(media)
    return out


# ----------------------------------------------------------------------
# Persistence pass
# ----------------------------------------------------------------------


async def _persist_media(
    medias: List[Dict[str, Any]],
    *,
    author_id: int,
    job: ScrapeJob,
    fetch_comments: bool,
    comment_limit: int,
    client,
    throttle: Throttle,
    incremental: bool,
) -> Dict[str, int]:
    """Filter + upsert each media; optionally fetch comments.

    Returns aggregate stats for the worker (api_calls, posts_saved,
    comments_saved, skipped_by_filter).
    """
    api_calls = 0
    posts_saved = 0
    comments_saved = 0
    skipped = 0

    since: Optional[datetime] = None
    params = job.params or {}
    if "since" in params and params["since"]:
        try:
            since = datetime.fromisoformat(params["since"])
            since = _to_naive_utc(since)
        except (TypeError, ValueError):
            since = None

    for media in medias:
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

        # Persist post (+ snapshot, hashtags, audio normalization).
        with session_scope() as session:
            audio_id = upsert_audio_track(session, media.get("music_info"))
            post_id = upsert_post(
                session,
                media=media,
                author_id=author_id,
                job_id=job.id,
                audio_track_id=audio_id,
            )
            posts_saved += 1

        # Fetch comments only when requested AND on full backfill OR
        # for posts we hadn't seen before (incremental walker only
        # collects newly-seen posts, so this branch is implicitly
        # "newly seen" on incrementals).
        if fetch_comments and comment_limit > 0:
            try:
                comments = await _fetch_comments(client, post_id, comment_limit)
                api_calls += 1
                with session_scope() as session:
                    comments_saved += upsert_comments(session, post_id, comments)
                await throttle.after_action("post")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "comment_fetch_failed",
                    post_id=post_id,
                    error=str(exc),
                )

    return {
        "api_calls": api_calls,
        "posts_saved": posts_saved,
        "comments_saved": comments_saved,
        "skipped_by_filter": skipped,
    }


# ----------------------------------------------------------------------
# Cursor update
# ----------------------------------------------------------------------


def _update_target_cursor(
    job: ScrapeJob,
    medias: List[Dict[str, Any]],
    *,
    full_backfill: bool,
) -> None:
    """Push the latest seen post_id / taken_at back to the scan_target."""
    if job.scan_target_id is None or not medias:
        return
    latest = max(
        medias,
        key=lambda m: _to_naive_utc(m.get("taken_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    pk = int(latest.get("pk") or latest.get("id") or 0)
    taken_at = _to_naive_utc(latest.get("taken_at"))
    now = datetime.now(timezone.utc)

    from sqlalchemy import text as sql_text

    with session_scope() as session:
        session.execute(
            sql_text(
                """
                UPDATE ig_scan_targets
                SET last_seen_post_id  = COALESCE(:pk, last_seen_post_id),
                    last_seen_taken_at = COALESCE(:taken_at, last_seen_taken_at),
                    last_run_at        = :now,
                    last_run_job_id    = :job_id,
                    first_backfill_done = first_backfill_done OR :full,
                    next_run_at        = :now + (interval_hours || ' hours')::interval
                                          + (RANDOM() * (interval_hours || ' hours')::interval * :jitter_pct / 100.0)
                                          - ((interval_hours || ' hours')::interval * :jitter_pct / 200.0),
                    updated_at         = :now
                WHERE id = :target_id
                """
            ),
            {
                "pk": pk,
                "taken_at": taken_at,
                "now": now,
                "job_id": job.id,
                "full": full_backfill,
                "target_id": job.scan_target_id,
                "jitter_pct": settings.IG_TARGET_INTERVAL_JITTER_PCT,
            },
        )


# ----------------------------------------------------------------------
# Public scraper entry points
# ----------------------------------------------------------------------


async def _run(
    job: ScrapeJob,
    account: Account,
    proxy: Optional[Proxy],
    *,
    full_backfill: bool,
) -> ScrapeResult:
    """Shared body for full + incremental — only the stop condition and
    backfill flag differ."""
    params = job.params or {}
    fetch_comments_flag = bool(params.get("fetch_comments", False))
    comment_limit = int(params.get("comment_limit", settings.IG_COMMENT_DEFAULT_LIMIT))

    throttle = Throttle()

    try:
        client = _build_authenticated_client(account, proxy)
    except RuntimeError as exc:
        return ScrapeResult(outcome="fatal", error=str(exc))

    try:
        # Resolve target → user_id and refresh profile.
        user_id = await _fetch_user_id(client, job.target)
        await throttle.after_action("profile")

        user_info = await _fetch_user_info(client, user_id)
        await throttle.after_action("profile")

        with session_scope() as session:
            upsert_ig_user(session, {**user_info, "raw": user_info})

        # Stop condition for incremental runs.
        stop_post_id: Optional[int] = None
        stop_taken_at: Optional[datetime] = None
        if not full_backfill:
            from sqlalchemy import text as sql_text

            if job.scan_target_id is not None:
                with session_scope() as session:
                    row = session.execute(
                        sql_text(
                            "SELECT last_seen_post_id, last_seen_taken_at "
                            "FROM ig_scan_targets WHERE id = :id"
                        ),
                        {"id": job.scan_target_id},
                    ).first()
                if row is not None:
                    stop_post_id = row[0]
                    stop_taken_at = _to_naive_utc(row[1])

        # See hikerapi/user_feed.py for the same logic — page-based defaults
        # configured via env so ops can reason in pages, not posts.
        _default_pages = (
            settings.IG_FULL_BACKFILL_DEFAULT_PAGES
            if full_backfill
            else settings.IG_INCREMENTAL_DEFAULT_PAGES
        )
        _default_max = _default_pages * settings.IG_DEFAULT_PAGE_SIZE
        hard_cap = min(
            settings.IG_MAX_POSTS_PER_JOB,
            int(params.get("max_posts", _default_max)),
        )

        # Walk feed + clips; each walk's mid-flight failure is caught so a
        # rate-limit on the second walk doesn't discard the first. The
        # persist step then runs against whatever we collected.
        feed: List[Dict[str, Any]] = []
        clips: List[Dict[str, Any]] = []
        partial_failure_exc: Optional[Exception] = None
        try:
            feed = await _walk_feed(
                client,
                user_id,
                fetcher=_fetch_feed_page,
                stop_at_post_id=stop_post_id,
                stop_at_taken_at=stop_taken_at,
                hard_cap=hard_cap,
                throttle=throttle,
            )
        except Exception as exc:  # noqa: BLE001
            partial_failure_exc = exc
            logger.warning(
                "instagrapi_feed_walk_interrupted",
                error=str(exc),
                collected=len(feed),
            )

        if partial_failure_exc is None:
            try:
                clips = await _walk_feed(
                    client,
                    user_id,
                    fetcher=_fetch_clips_page,
                    stop_at_post_id=stop_post_id,
                    stop_at_taken_at=stop_taken_at,
                    hard_cap=hard_cap,
                    throttle=throttle,
                )
            except Exception as exc:  # noqa: BLE001
                partial_failure_exc = exc
                logger.warning(
                    "instagrapi_clips_walk_interrupted",
                    error=str(exc),
                    collected=len(clips),
                )

        merged = _merge_dedupe(feed, clips)
        api_calls_walk = 1 + len(feed) // 50 + len(clips) // 50  # rough; throttle counts the rest

        # Always persist what we collected, even on partial failure. That's
        # the whole point — buffered pagination must not throw away pages
        # already on the wire.
        stats = await _persist_media(
            merged,
            author_id=user_id,
            job=job,
            fetch_comments=fetch_comments_flag,
            comment_limit=comment_limit,
            client=client,
            throttle=throttle,
            incremental=not full_backfill,
        )

        _update_target_cursor(job, merged, full_backfill=full_backfill)

        result_stats = {
            "posts_seen": len(merged),
            "posts_saved": stats["posts_saved"],
            "comments_saved": stats["comments_saved"],
            "skipped_by_filter": stats["skipped_by_filter"],
            "feed_pages": len(feed),
            "clip_pages": len(clips),
        }

        if partial_failure_exc is not None:
            outcome = _classify_runtime_exception(partial_failure_exc)
            result_stats["partial"] = True
            return ScrapeResult(
                outcome=outcome,
                api_calls=stats["api_calls"] + api_calls_walk + 2,
                posts_saved=stats["posts_saved"],
                comments_saved=stats["comments_saved"],
                stories_saved=0,
                error=f"{type(partial_failure_exc).__name__}: {partial_failure_exc}",
                stats=result_stats,
            )

        return ScrapeResult(
            outcome="success",
            api_calls=stats["api_calls"] + api_calls_walk + 2,  # +profile +user_info
            posts_saved=stats["posts_saved"],
            comments_saved=stats["comments_saved"],
            stories_saved=0,
            stats=result_stats,
        )
    except Exception as exc:  # noqa: BLE001
        outcome = _classify_runtime_exception(exc)
        return ScrapeResult(outcome=outcome, error=f"{type(exc).__name__}: {exc}")


def _classify_runtime_exception(exc: Exception) -> str:
    """Map instagrapi runtime exceptions to a worker outcome."""
    name = type(exc).__name__
    if name in {"PleaseWaitFewMinutes", "RateLimitError"}:
        return "rate_limited"
    if name in {"ChallengeRequired", "FeedbackRequired"}:
        return "challenge"
    if name in {"UserNotFound"}:
        return "fatal"
    return "soft_fail"


async def run_user_feed_full(job: ScrapeJob, account: Account, proxy: Optional[Proxy]) -> ScrapeResult:
    """First-time backfill of a username target."""
    return await _run(job, account, proxy, full_backfill=True)


async def run_user_feed_incremental(
    job: ScrapeJob, account: Account, proxy: Optional[Proxy]
) -> ScrapeResult:
    """Daily delta — stops at the scan_target cursor."""
    return await _run(job, account, proxy, full_backfill=False)
