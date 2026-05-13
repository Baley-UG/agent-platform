"""Read-only Instagram users API.

Wraps the `ig_users` table — public Instagram profiles we've persisted
as a side effect of scraping posts / stories / hashtag feeds. Not to be
confused with `ig_accounts` (the *scraping* accounts we control), or
the platform's `public.user` table (admin panel users).

Endpoints:
    GET /users                  — filterable / sortable list
    GET /users/{username}       — full profile + aggregate post stats
    GET /users/{username}/posts — convenience alias for /posts?author=…
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.posts import PostListItem
from app.schemas.users import IgUserDetail, IgUserSummary
from app.services import queries
from app.services.database import read_session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get(
    "",
    response_model=List[IgUserSummary],
    summary="List scraped Instagram profiles",
)
def list_users(
    search: Optional[str] = Query(
        default=None,
        description="Substring match on username + full_name (case-insensitive).",
    ),
    min_followers: Optional[int] = Query(default=None, ge=0),
    is_business: Optional[bool] = Query(default=None),
    is_verified: Optional[bool] = Query(default=None),
    is_private: Optional[bool] = Query(default=None),
    order: str = Query(
        default="follower_count_desc",
        pattern="^(follower_count_desc|media_count_desc|last_seen_desc|first_seen_desc|username_asc)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> List[IgUserSummary]:
    """Filterable directory of every Instagram profile we've persisted."""
    with read_session_scope() as session:
        return queries.list_ig_users(
            session,
            search=search,
            min_followers=min_followers,
            is_business=is_business,
            is_verified=is_verified,
            is_private=is_private,
            order=order,
            limit=limit,
            offset=offset,
        )


@router.get(
    "/{username}",
    response_model=IgUserDetail,
    summary="Profile detail + aggregate post stats",
    responses={404: {"description": "user not in ig_users"}},
)
def user_detail(username: str) -> IgUserDetail:
    """Last-seen profile snapshot, raw HikerAPI payload, and rolled-up
    stats from `ig_posts` (post count, avg likes / plays / score)."""
    with read_session_scope() as session:
        row = queries.get_ig_user_detail(session, username)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user '{username}' not in ig_users",
        )
    return row


@router.get(
    "/{username}/posts",
    response_model=List[PostListItem],
    summary="Posts by this user (convenience alias for /posts?author=…)",
)
def user_posts(
    username: str,
    min_likes: Optional[int] = Query(default=None, ge=0),
    min_play_count: Optional[int] = Query(default=None, ge=0),
    min_score: Optional[float] = Query(default=None, ge=0, le=100),
    since: Optional[datetime] = Query(default=None),
    order: str = Query(
        default="taken_at_desc",
        pattern="^(taken_at_desc|score_desc|likes_desc|play_count_desc)$",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[PostListItem]:
    """Reuses the same query layer as `/posts` for a consistent shape."""
    with read_session_scope() as session:
        if order == "score_desc":
            rows = queries.get_user_top_posts(
                session, username=username, by="score", since=since, limit=limit
            )
        elif order == "likes_desc":
            rows = queries.get_user_top_posts(
                session, username=username, by="likes", since=since, limit=limit
            )
        elif order == "play_count_desc":
            rows = queries.get_user_top_posts(
                session, username=username, by="play_count", since=since, limit=limit
            )
        else:
            rows = queries.search_posts(
                session,
                author=username,
                min_likes=min_likes,
                min_play_count=min_play_count,
                since=since,
                limit=limit,
            )
        if min_likes is not None:
            rows = [r for r in rows if (r.get("like_count") or 0) >= min_likes]
        if min_play_count is not None:
            rows = [
                r
                for r in rows
                if max(r.get("play_count") or 0, r.get("view_count") or 0) >= min_play_count
            ]
        if min_score is not None:
            rows = [r for r in rows if (r.get("score") or 0) >= min_score]
        return rows
