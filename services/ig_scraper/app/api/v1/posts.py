"""Read-only posts API.

Adds operator-friendly REST access for content queries that the AI
generation pipeline (and the Grafana dashboards from M10) will lean on.
Read tools on MCP share the same query layer (`app.services.queries`)
so we don't duplicate filter logic.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.services import queries
from app.services.database import read_session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("", response_model=List[Dict[str, Any]])
def list_posts(
    author: Optional[str] = Query(default=None, description="Username (without @)."),
    hashtag: Optional[str] = Query(default=None, description="Tag name (without #)."),
    min_likes: Optional[int] = Query(default=None, ge=0),
    min_play_count: Optional[int] = Query(default=None, ge=0),
    min_score: Optional[float] = Query(default=None, ge=0, le=100),
    since: Optional[datetime] = Query(default=None),
    order: str = Query(default="taken_at_desc", pattern="^(taken_at_desc|score_desc|likes_desc|play_count_desc)$"),
    limit: int = Query(default=50, ge=1, le=500),
) -> List[Dict[str, Any]]:
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


@router.get("/{post_id}/comments", response_model=List[Dict[str, Any]])
def post_comments(post_id: int, limit: int = Query(default=50, ge=1, le=500)) -> List[Dict[str, Any]]:
    """Comments on a single post."""
    with read_session_scope() as session:
        return queries.get_post_comments(session, post_id, limit=limit)
