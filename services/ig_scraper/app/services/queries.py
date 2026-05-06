"""Read-only query helpers for the MCP / read-only API surface.

These don't mutate state and live separately from the upsert-heavy
persistence layer so they can be pointed at the read replica when one
is configured.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlmodel import Session


def search_posts(
    session: Session,
    *,
    author: Optional[str] = None,
    hashtag: Optional[str] = None,
    min_likes: Optional[int] = None,
    min_play_count: Optional[int] = None,
    since: Optional[datetime] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Query `ig_posts` joined to authors + hashtags. Returns POJOs.

    The result shape stays small on purpose — caller's MCP tool returns
    it as JSON. Heavy fields like `raw` and `media_urls` are kept off
    the wire by default.
    """
    where: List[str] = []
    params: Dict[str, Any] = {"limit": int(min(max(limit, 1), 200))}
    if author:
        where.append("u.username = :author")
        params["author"] = author.lower().lstrip("@")
    if hashtag:
        where.append("EXISTS (SELECT 1 FROM ig_post_hashtags h "
                     "WHERE h.post_id = p.id AND h.hashtag = :hashtag)")
        params["hashtag"] = hashtag.lower().lstrip("#")
    if min_likes is not None:
        where.append("p.like_count >= :min_likes")
        params["min_likes"] = int(min_likes)
    if min_play_count is not None:
        where.append("COALESCE(p.play_count, p.view_count, 0) >= :min_play_count")
        params["min_play_count"] = int(min_play_count)
    if since is not None:
        where.append("p.taken_at >= :since")
        params["since"] = since

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = session.execute(
        text(
            f"""
            SELECT
                p.id, p.code, p.media_type, p.product_type, p.taken_at,
                p.like_count, p.comment_count, p.play_count, p.view_count,
                p.caption, p.thumbnail_url, p.score,
                u.username AS author_username
            FROM ig_posts p
            JOIN ig_users u ON u.id = p.author_id
            {where_sql}
            ORDER BY p.taken_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def get_user_top_posts(
    session: Session,
    *,
    username: str,
    by: str = "likes",
    since: Optional[datetime] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Top posts for a single author, ordered by likes / play_count / comments / score."""
    order_col = {
        "likes": "p.like_count",
        "play_count": "COALESCE(p.play_count, p.view_count, 0)",
        "comments": "p.comment_count",
        "score": "COALESCE(p.score, 0)",
    }.get(by, "p.like_count")

    params: Dict[str, Any] = {
        "username": username.lower().lstrip("@"),
        "limit": int(min(max(limit, 1), 200)),
    }
    extra_where = ""
    if since is not None:
        extra_where = "AND p.taken_at >= :since"
        params["since"] = since

    rows = session.execute(
        text(
            f"""
            SELECT
                p.id, p.code, p.media_type, p.product_type, p.taken_at,
                p.like_count, p.comment_count, p.play_count, p.view_count,
                p.caption, p.thumbnail_url, p.score
            FROM ig_posts p
            JOIN ig_users u ON u.id = p.author_id
            WHERE u.username = :username {extra_where}
            ORDER BY {order_col} DESC NULLS LAST
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def get_user_profile(session: Session, username: str) -> Optional[Dict[str, Any]]:
    """Last known profile snapshot. Returns None if we've never seen them."""
    row = session.execute(
        text(
            """
            SELECT id, username, full_name, biography, follower_count,
                   following_count, media_count, is_business, is_verified,
                   is_private, profile_pic_url, first_seen_at, last_seen_at
            FROM ig_users
            WHERE username = :username
            """
        ),
        {"username": username.lower().lstrip("@")},
    ).mappings().first()
    return dict(row) if row else None


def get_post_comments(
    session: Session, post_id: int, limit: int = 50
) -> List[Dict[str, Any]]:
    """Comments for a post — paginated by like_count desc."""
    rows = session.execute(
        text(
            """
            SELECT
                c.id, c.text, c.like_count, c.created_at_ig,
                u.username AS author_username
            FROM ig_comments c
            JOIN ig_users u ON u.id = c.author_id
            WHERE c.post_id = :post_id
            ORDER BY c.like_count DESC, c.created_at_ig DESC NULLS LAST
            LIMIT :limit
            """
        ),
        {"post_id": int(post_id), "limit": int(min(max(limit, 1), 500))},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_recent_stories(
    session: Session, *, username: str, since: Optional[datetime] = None, limit: int = 50
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "username": username.lower().lstrip("@"),
        "limit": int(min(max(limit, 1), 500)),
    }
    extra = ""
    if since is not None:
        extra = "AND s.taken_at >= :since"
        params["since"] = since
    rows = session.execute(
        text(
            f"""
            SELECT
                s.id, s.media_type, s.taken_at, s.expires_at,
                s.media_url, s.thumbnail_url, s.caption,
                s.mentions, s.hashtags, s.link_sticker_url
            FROM ig_stories s
            JOIN ig_users u ON u.id = s.author_id
            WHERE u.username = :username {extra}
            ORDER BY s.taken_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def get_job_status(session: Session, job_id) -> Optional[Dict[str, Any]]:
    row = session.execute(
        text(
            """
            SELECT id, job_type, target, status, attempt, max_attempts,
                   error, stats, started_at, finished_at, created_at
            FROM ig_scrape_jobs
            WHERE id = :job_id
            """
        ),
        {"job_id": job_id},
    ).mappings().first()
    return dict(row) if row else None
