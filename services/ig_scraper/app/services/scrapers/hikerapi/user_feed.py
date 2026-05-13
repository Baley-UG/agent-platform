"""HikerAPI port of user_feed_full / user_feed_incremental.

Endpoints used (verified against the public OpenAPI spec at
https://api.hikerapi.com/openapi.json):

  GET /v2/user/by/username?username=<u>         → user object
  GET /v1/user/medias/chunk?user_id=<id>&end_cursor=<c>  → posts page
  GET /v1/media/comments/chunk?id=<media_id>&end_cursor=<c> → comments

Note: medias and comments live in v1, not v2 — the v2 namespace doesn't
expose paginated chunk variants for them. We mix-and-match: v2 for the
single-resource endpoints (user/by/username), v1 for the chunked
collection endpoints. The response field names (`pk`, `code`,
`like_count`, etc.) are identical across versions.

We reuse the existing persistence layer (upsert_ig_user, upsert_post,
upsert_comments, etc.) so HikerAPI rows look identical to instagrapi
rows in the database.

Cursor logic mirrors the instagrapi version: incremental walks stop
when we hit `last_seen_post_id` or `last_seen_taken_at`.
"""

import uuid
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
    upsert_comments,
    upsert_ig_user,
    upsert_post,
)
from app.services.scrapers import ScrapeResult
from app.services.scrapers.hikerapi.client import (
    HikerAPIClient,
    HikerAPIError,
    HikerAPINotFound,
    HikerAPIQuotaExceeded,
)


def _to_naive_utc(value: Any) -> Optional[datetime]:
    """Coerce HikerAPI's mixed datetime / unix-epoch values to UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalise_media(media: Dict[str, Any]) -> Dict[str, Any]:
    """Bridge HikerAPI's field names to what `upsert_post` expects.

    HikerAPI returns IG-compatible objects with `pk`, `code`, `media_type`,
    `caption_text`, `like_count`, etc. — already the same shape instagrapi
    returns. We add `id` as alias for `pk` so the persistence layer's
    primary-key extraction works regardless of which client produced the row.
    """
    if "pk" in media and "id" not in media:
        media["id"] = media["pk"]
    return media


def _unwrap(payload: Any, expected_keys: tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """HikerAPI wraps responses inconsistently across endpoints.

    Some return the object flat:
        {"pk": "...", "username": "..."}
    Others wrap under a key:
        {"user": {...}} or {"data": {...}} or {"result": {...}}

    `expected_keys` is the set of field names we'd see on a "flat"
    response (e.g. ("pk", "id") for user, ("medias",) for a media
    list). We probe a handful of common wrapper keys and unwrap once.
    """
    if not isinstance(payload, dict):
        return None
    if any(k in payload for k in expected_keys):
        return payload
    for wrapper in ("user", "data", "result", "response"):
        candidate = payload.get(wrapper)
        if isinstance(candidate, dict) and any(k in candidate for k in expected_keys):
            return candidate
    return None


async def _fetch_user(client: HikerAPIClient, username: str) -> Optional[Dict[str, Any]]:
    try:
        raw = await client.get("/v2/user/by/username", username=username)
    except HikerAPINotFound:
        return None
    user = _unwrap(raw, ("pk", "id"))
    if user is None:
        # First-time integration debug aid: dump the raw shape so the
        # operator can adjust _unwrap() or change the endpoint path.
        sample = str(raw)[:500] if raw is not None else "<None>"
        logger.error("hikerapi_user_payload_unrecognised", username=username, sample=sample)
    return user


async def _iter_medias(
    client: HikerAPIClient,
    *,
    user_id: int,
    stop_at_post_id: Optional[int],
    stop_at_taken_at: Optional[datetime],
    hard_cap: int,
):
    """Yield each normalised media as it arrives from HikerAPI.

    Important: this is a streaming iterator, NOT a buffer. The caller
    persists each media inside its own loop iteration, so a mid-flight
    402/429 keeps everything saved so far. The old buffered version lost
    all data on partial failure.
    """

    def _stop(media: Dict[str, Any]) -> bool:
        media_id = int(media.get("pk") or media.get("id") or 0)
        if stop_at_post_id and media_id == stop_at_post_id:
            return True
        taken = _to_naive_utc(media.get("taken_at"))
        if stop_at_taken_at and taken and taken <= stop_at_taken_at:
            return True
        return False

    async for media in client.paginate_chunks(
        "/v1/user/medias/chunk",
        items_key="response",
        max_items=hard_cap,
        stop_when=_stop,
        user_id=user_id,
    ):
        # v1/user/medias/chunk wraps each item in {"pk", ...} but the
        # outer payload uses "response" as the list key on some HikerAPI
        # versions. paginate_chunks already tolerates a missing key (no
        # items → empty page → loop ends), so this is the right value.
        yield _normalise_media(media)


async def _fetch_comments(
    client: HikerAPIClient, media_id: int, limit: int
) -> List[Dict[str, Any]]:
    """Fetch up to `limit` comments for a media id.

    `/v1/media/comments/chunk` per openapi.json:
      - Required: `id=<media_id>`
      - Optional pagination: `max_id` / `min_id` (NOT `end_cursor`)
      - Response: bare `Array<object>` (no wrapper, no cursor element)

    paginate_chunks sees a bare array and returns after the first page —
    which is what we want most of the time: comment fetching defaults to
    OFF (fetch_comments=false), so this code path rarely fires, and when
    it does we cap at IG_COMMENT_DEFAULT_LIMIT (~50) anyway. If deep
    comment pagination becomes a need, switch to a dedicated walker that
    threads `max_id` through successive calls.
    """
    if limit <= 0:
        return []
    out: List[Dict[str, Any]] = []
    async for comment in client.paginate_chunks(
        "/v1/media/comments/chunk",
        items_key="response",
        max_items=limit,
        id=media_id,
    ):
        out.append(comment)
    return out


def _resolve_target_cursor(scan_target_id: Optional[uuid.UUID]) -> tuple[Optional[int], Optional[datetime]]:
    """Read the existing cursor off ig_scan_targets for incremental runs."""
    if scan_target_id is None:
        return None, None
    from sqlalchemy import text as sql_text

    with session_scope() as session:
        row = session.execute(
            sql_text(
                "SELECT last_seen_post_id, last_seen_taken_at "
                "FROM ig_scan_targets WHERE id = :id"
            ),
            {"id": scan_target_id},
        ).first()
    if row is None:
        return None, None
    return row[0], _to_naive_utc(row[1])


def _update_cursor_after_run(
    job: ScrapeJob,
    medias: List[Dict[str, Any]],
    *,
    full_backfill: bool,
) -> None:
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
                SET last_seen_post_id   = COALESCE(:pk, last_seen_post_id),
                    last_seen_taken_at  = COALESCE(:taken_at, last_seen_taken_at),
                    last_run_at         = :now,
                    last_run_job_id     = :job_id,
                    first_backfill_done = first_backfill_done OR :full,
                    next_run_at         = :now + (interval_hours || ' hours')::interval
                                          + (RANDOM() * (interval_hours || ' hours')::interval * :jitter_pct / 100.0)
                                          - ((interval_hours || ' hours')::interval * :jitter_pct / 200.0),
                    updated_at          = :now
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


def _classify_hikerapi_error(exc: Exception) -> str:
    """Map HikerAPI exceptions to ScrapeResult outcomes."""
    if isinstance(exc, HikerAPIQuotaExceeded):
        return "rate_limited"
    if isinstance(exc, HikerAPINotFound):
        return "fatal"
    return "soft_fail"


async def _run(
    job: ScrapeJob,
    account: Optional[Account],
    proxy: Optional[Proxy],
    *,
    full_backfill: bool,
) -> ScrapeResult:
    params = job.params or {}
    fetch_comments_flag = bool(params.get("fetch_comments", False))
    comment_limit = int(params.get("comment_limit", settings.IG_COMMENT_DEFAULT_LIMIT))
    # `max_posts` is honoured for both full and incremental backfills so
    # the caller can say "just grab the latest 30 posts" without writing
    # a custom job type. Capped at IG_MAX_POSTS_PER_JOB so a typo can't
    # nuke the budget. Defaults are EXPRESSED IN PAGES via env so ops
    # can reason about "3 pages" not "150 posts":
    #   - Full backfill default: IG_FULL_BACKFILL_DEFAULT_PAGES × page_size
    #   - Incremental default:   IG_INCREMENTAL_DEFAULT_PAGES × page_size
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

    since: Optional[datetime] = None
    if params.get("since"):
        try:
            since = _to_naive_utc(datetime.fromisoformat(params["since"]))
        except (TypeError, ValueError):
            since = None

    api_calls = 0
    posts_saved = 0
    comments_saved = 0
    skipped = 0

    try:
        async with HikerAPIClient() as client:
            user_payload = await _fetch_user(client, job.target)
            api_calls += 1
            if user_payload is None:
                return ScrapeResult(
                    outcome="fatal",
                    api_calls=api_calls,
                    error=(
                        f"User '{job.target}' returned no recognisable payload "
                        f"from HikerAPI. Either the user doesn't exist (404), "
                        f"or the response shape changed — check worker logs "
                        f"for 'hikerapi_user_payload_unrecognised' details."
                    ),
                )
            user_pk_raw = user_payload.get("pk") or user_payload.get("id")
            try:
                user_id = int(user_pk_raw)
            except (TypeError, ValueError):
                logger.error(
                    "hikerapi_user_pk_not_numeric",
                    target=job.target,
                    pk_value=user_pk_raw,
                    payload_sample=str(user_payload)[:300],
                )
                return ScrapeResult(
                    outcome="fatal",
                    api_calls=api_calls,
                    error=f"User '{job.target}' returned non-numeric pk: {user_pk_raw!r}",
                )

            with session_scope() as session:
                upsert_ig_user(
                    session,
                    {**user_payload, "id": user_id, "raw": user_payload},
                )

            stop_post_id, stop_taken_at = (
                (None, None) if full_backfill else _resolve_target_cursor(job.scan_target_id)
            )

            # STREAMING save loop: each media is persisted as it lands.
            # A mid-flight 402/429 leaves everything-so-far in the DB,
            # instead of discarding hundreds of fetched pages.
            seen_medias: List[Dict[str, Any]] = []
            partial_failure_exc: Optional[HikerAPIError] = None
            try:
                async for media in _iter_medias(
                    client,
                    user_id=user_id,
                    stop_at_post_id=stop_post_id,
                    stop_at_taken_at=stop_taken_at,
                    hard_cap=hard_cap,
                ):
                    seen_medias.append(media)
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
                        audio_id = upsert_audio_track(session, media.get("music_info"))
                        post_id = upsert_post(
                            session,
                            media=media,
                            author_id=user_id,
                            job_id=job.id,
                            audio_track_id=audio_id,
                        )
                        posts_saved += 1

                    if fetch_comments_flag and comment_limit > 0:
                        try:
                            comments = await _fetch_comments(client, post_id, comment_limit)
                            api_calls += max(1, (len(comments) // settings.HIKERAPI_PAGE_SIZE) + 1)
                            with session_scope() as session:
                                comments_saved += upsert_comments(session, post_id, comments)
                        except HikerAPIError as exc:
                            logger.warning(
                                "hikerapi_comment_fetch_failed",
                                post_id=post_id,
                                error=str(exc),
                            )
            except HikerAPIError as exc:
                # Pagination died mid-flight. Everything we've persisted
                # above stays. Surface the partial outcome below.
                partial_failure_exc = exc
                logger.warning(
                    "hikerapi_pagination_interrupted",
                    error=str(exc),
                    posts_saved=posts_saved,
                    posts_seen=len(seen_medias),
                )

            api_calls += max(1, (len(seen_medias) // settings.HIKERAPI_PAGE_SIZE) + 1)
            _update_cursor_after_run(job, seen_medias, full_backfill=full_backfill)

        if partial_failure_exc is not None:
            return ScrapeResult(
                outcome=_classify_hikerapi_error(partial_failure_exc),
                api_calls=api_calls,
                posts_saved=posts_saved,
                comments_saved=comments_saved,
                stories_saved=0,
                error=f"{type(partial_failure_exc).__name__}: {partial_failure_exc}",
                stats={
                    "posts_seen": len(seen_medias),
                    "posts_saved": posts_saved,
                    "comments_saved": comments_saved,
                    "skipped_by_filter": skipped,
                    "source": "hikerapi",
                    "partial": True,
                },
            )

        return ScrapeResult(
            outcome="success",
            api_calls=api_calls,
            posts_saved=posts_saved,
            comments_saved=comments_saved,
            stories_saved=0,
            stats={
                "posts_seen": len(seen_medias),
                "posts_saved": posts_saved,
                "comments_saved": comments_saved,
                "skipped_by_filter": skipped,
                "source": "hikerapi",
            },
        )
    except HikerAPIError as exc:
        return ScrapeResult(
            outcome=_classify_hikerapi_error(exc),
            api_calls=api_calls,
            error=f"{type(exc).__name__}: {exc}",
        )


async def run_user_feed_full_hk(
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    """First-time backfill via HikerAPI."""
    return await _run(job, account, proxy, full_backfill=True)


async def run_user_feed_incremental_hk(
    job: ScrapeJob, account: Optional[Account], proxy: Optional[Proxy]
) -> ScrapeResult:
    """Daily delta — stops at scan_target cursor."""
    return await _run(job, account, proxy, full_backfill=False)
