"""Read-only posts API.

Adds operator-friendly REST access for content queries that the AI
generation pipeline (and the Grafana dashboards from M10) will lean on.
Read tools on MCP share the same query layer (`app.services.queries`)
so we don't duplicate filter logic.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.posts import PostCommentItem, PostDetail, PostListItem
from app.services import queries
from app.services.database import read_session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get(
    "",
    response_model=List[PostListItem],
    summary="List Instagram posts (filterable)",
)
def list_posts(
    author: Optional[str] = Query(default=None, description="Username (without @)."),
    hashtag: Optional[str] = Query(default=None, description="Tag name (without #)."),
    min_likes: Optional[int] = Query(default=None, ge=0),
    min_play_count: Optional[int] = Query(default=None, ge=0),
    min_score: Optional[float] = Query(default=None, ge=0, le=100),
    since: Optional[datetime] = Query(default=None),
    order: str = Query(default="taken_at_desc", pattern="^(taken_at_desc|score_desc|likes_desc|play_count_desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[PostListItem]:
    """Filterable post list.

    `order=score_desc` is the canonical "best content" query for the AI
    generator. `min_score` works alongside any of the other filters.
    """
    if order == "score_desc":
        # Sorting by score isn't covered by `search_posts` (which
        # always orders by taken_at). For score-ordered queries we
        # call get_user_top_posts when an author is given, otherwise
        # fall through to the general path with a manual filter.
        with read_session_scope() as session:
            if author:
                rows = queries.get_user_top_posts(
                    session, username=author, by="score", since=since, limit=limit
                )
            else:
                rows = queries.search_posts(
                    session,
                    author=None,
                    hashtag=hashtag,
                    min_likes=min_likes,
                    min_play_count=min_play_count,
                    since=since,
                    limit=limit * 4,  # over-fetch so post-filter has room
                )
                rows = sorted(
                    rows,
                    key=lambda r: (r.get("score") or 0),
                    reverse=True,
                )[:limit]
            if min_score is not None:
                rows = [r for r in rows if (r.get("score") or 0) >= min_score]
            return rows

    if order == "likes_desc" and author:
        with read_session_scope() as session:
            rows = queries.get_user_top_posts(
                session, username=author, by="likes", since=since, limit=limit
            )
            if min_score is not None:
                rows = [r for r in rows if (r.get("score") or 0) >= min_score]
            return rows

    if order == "play_count_desc" and author:
        with read_session_scope() as session:
            rows = queries.get_user_top_posts(
                session, username=author, by="play_count", since=since, limit=limit
            )
            if min_score is not None:
                rows = [r for r in rows if (r.get("score") or 0) >= min_score]
            return rows

    # Default path: taken_at_desc.
    with read_session_scope() as session:
        rows = queries.search_posts(
            session,
            author=author,
            hashtag=hashtag,
            min_likes=min_likes,
            min_play_count=min_play_count,
            since=since,
            limit=limit,
        )
        if min_score is not None:
            rows = [r for r in rows if (r.get("score") or 0) >= min_score]
        return rows


@router.get(
    "/{post_id}",
    response_model=PostDetail,
    summary="Detailed view of a single post",
    responses={404: {"description": "post not found"}},
)
def post_detail(
    post_id: int,
    include_comments: int = Query(
        default=0,
        ge=0,
        le=500,
        description="Include top N comments inline (0 = none, default).",
    ),
) -> PostDetail:
    """Detailed view of a single post.

    Returns every column on `ig_posts` plus the author's profile fields,
    parsed caption features (language / emoji_count / has_question /
    has_cta / hashtags / mentions), media URLs, music info, location,
    and score components. Set `include_comments=N` to inline the top N
    comments by like_count.
    """
    with read_session_scope() as session:
        row = queries.get_post_detail(session, post_id, include_comments=include_comments)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"post {post_id} not found",
        )
    return row


@router.get(
    "/{post_id}/comments",
    response_model=List[PostCommentItem],
    summary="Comments on a single post (top-N by like_count)",
)
def post_comments(post_id: int, limit: int = Query(default=50, ge=1, le=500)) -> List[PostCommentItem]:
    """Top comments by like_count for the given post."""
    with read_session_scope() as session:
        return queries.get_post_comments(session, post_id, limit=limit)


# ----- Mirror endpoints (lazy on-view + manual pin) -----


@router.get(
    "/{post_id}/preview-url",
    summary="Presigned URL for the post's media (lazy-mirrors on first call)",
)
async def post_preview_url(
    post_id: int,
    ttl: int = Query(default=3600, ge=60, le=86400),
    refetch: bool = Query(
        default=False,
        description=(
            "When the S3 mirror is missing AND the cached IG URL has "
            "expired, hitting HikerAPI for a fresh signed URL costs 1 "
            "credit. Opt in with `refetch=true`."
        ),
    ),
):
    """Resolve and return short-lived URLs for the post's media + poster.

    Order:
      1. `media_s3_key` set → presign our S3 (free, always works).
      2. Else, cached IG URL still alive → mirror now, then presign.
      3. Else, `refetch=true` → HikerAPI /v1/media/by/id → mirror → presign.
    """
    from sqlalchemy import text as sql_text
    from app.core import s3 as s3lib
    from app.services import mirror as mirror_svc
    from app.services.database import session_scope

    with session_scope() as session:
        row = (
            session.execute(
                sql_text(
                    "SELECT id, media_s3_key, poster_s3_key, media_urls, thumbnail_url "
                    "FROM ig_posts WHERE id = :pid"
                ),
                {"pid": int(post_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"post {post_id} not found"
            )

        media_key = row["media_s3_key"]
        poster_key = row["poster_s3_key"]
        urls = list(row["media_urls"] or [])
        thumb = row["thumbnail_url"]

        # Step 2: lazy mirror via cached IG URL.
        if not media_key and urls:
            mk, pk = mirror_svc.mirror_post_media(
                session, post_id=int(post_id), media_urls=urls, thumbnail_url=thumb
            )
            media_key = mk or media_key
            poster_key = pk or poster_key
            session.commit()

        # Step 3: optional HikerAPI re-fetch.
        refetched = False
        if not media_key and refetch:
            payload = await mirror_svc.refetch_via_hikerapi(int(post_id))
            if payload:
                from app.services.persistence.posts import _collect_media_urls

                fresh_urls = _collect_media_urls(payload)
                fresh_thumb = payload.get("thumbnail_url") or thumb
                if fresh_urls:
                    mk, pk = mirror_svc.mirror_post_media(
                        session,
                        post_id=int(post_id),
                        media_urls=fresh_urls,
                        thumbnail_url=fresh_thumb,
                    )
                    media_key = mk or media_key
                    poster_key = pk or poster_key
                    refetched = True
                    session.commit()

        s3_ok = s3lib.is_configured()
        return {
            "media_url": s3lib.presigned_get_url(media_key, ttl=ttl) if media_key and s3_ok else None,
            "poster_url": s3lib.presigned_get_url(poster_key, ttl=ttl) if poster_key and s3_ok else None,
            "fallback_ig_media_url": urls[0] if urls else None,
            "fallback_ig_thumbnail_url": thumb,
            "ttl_seconds": ttl,
            "mirror_pending": media_key is None,
            "refetched_via_hikerapi": refetched,
        }


@router.post(
    "/{post_id}/mirror",
    summary="Pin the post — force-mirror its media to S3 right now",
)
async def post_mirror_pin(post_id: int, refetch: bool = Query(default=False)):
    """Admin-triggered mirror. Re-attempts even when keys exist (overwrites)."""
    from sqlalchemy import text as sql_text
    from app.services import mirror as mirror_svc
    from app.services.database import session_scope

    with session_scope() as session:
        row = (
            session.execute(
                sql_text("SELECT id, media_urls, thumbnail_url FROM ig_posts WHERE id = :pid"),
                {"pid": int(post_id)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"post {post_id} not found"
            )

        urls = list(row["media_urls"] or [])
        thumb = row["thumbnail_url"]
        refetched = False
        if not urls and refetch:
            payload = await mirror_svc.refetch_via_hikerapi(int(post_id))
            if payload:
                from app.services.persistence.posts import _collect_media_urls

                urls = _collect_media_urls(payload)
                thumb = payload.get("thumbnail_url") or thumb
                refetched = True

        mk, pk = mirror_svc.mirror_post_media(
            session, post_id=int(post_id), media_urls=urls, thumbnail_url=thumb
        )
        session.commit()
        return {
            "media_s3_key": mk,
            "poster_s3_key": pk,
            "refetched_via_hikerapi": refetched,
            "ok": bool(mk or pk),
        }
