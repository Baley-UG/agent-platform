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
                p.caption, p.caption_length, p.language,
                p.hashtags, p.mentions,
                p.thumbnail_url, p.media_urls, p.video_duration,
                p.score,
                u.username AS author_username,
                u.full_name AS author_full_name,
                u.is_verified AS author_is_verified,
                u.follower_count AS author_follower_count,
                u.profile_pic_url AS author_profile_pic_url
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


def list_ig_users(
    session: Session,
    *,
    search: Optional[str] = None,
    min_followers: Optional[int] = None,
    is_business: Optional[bool] = None,
    is_verified: Optional[bool] = None,
    is_private: Optional[bool] = None,
    order: str = "follower_count_desc",
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Filterable list of scraped Instagram profiles (`ig_users`).

    `search` is a case-insensitive substring match over `username` and
    `full_name`. Sort options pin to whitelisted SQL columns so the
    param can never escape into a free-form ORDER BY.
    """
    where: List[str] = []
    params: Dict[str, Any] = {
        "limit": int(min(max(limit, 1), 500)),
        "offset": int(max(offset, 0)),
    }
    if search:
        where.append("(username ILIKE :search OR full_name ILIKE :search)")
        params["search"] = f"%{search.strip().lstrip('@')}%"
    if min_followers is not None:
        where.append("COALESCE(follower_count, 0) >= :min_followers")
        params["min_followers"] = int(min_followers)
    if is_business is not None:
        where.append("is_business = :is_business")
        params["is_business"] = bool(is_business)
    if is_verified is not None:
        where.append("is_verified = :is_verified")
        params["is_verified"] = bool(is_verified)
    if is_private is not None:
        where.append("is_private = :is_private")
        params["is_private"] = bool(is_private)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order_col = {
        "follower_count_desc": "COALESCE(follower_count, 0) DESC",
        "media_count_desc": "COALESCE(media_count, 0) DESC",
        "last_seen_desc": "last_seen_at DESC",
        "first_seen_desc": "first_seen_at DESC",
        "username_asc": "username ASC",
    }.get(order, "COALESCE(follower_count, 0) DESC")

    rows = session.execute(
        text(
            f"""
            SELECT id, username, full_name, biography, follower_count,
                   following_count, media_count, is_business, is_verified,
                   is_private, profile_pic_url, first_seen_at, last_seen_at
            FROM ig_users
            {where_sql}
            ORDER BY {order_col}
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()
    return [dict(r) for r in rows]


def get_ig_user_detail(session: Session, username: str) -> Optional[Dict[str, Any]]:
    """Full profile (incl. raw HikerAPI payload) + aggregate stats from `ig_posts`.

    Returns None when the user isn't in `ig_users`. The `stats` block is
    always present (zero-filled if we haven't scraped any of their posts).
    """
    profile = session.execute(
        text(
            """
            SELECT id, username, full_name, biography, follower_count,
                   following_count, media_count, is_business, is_verified,
                   is_private, profile_pic_url, raw,
                   first_seen_at, last_seen_at
            FROM ig_users
            WHERE username = :username
            """
        ),
        {"username": username.lower().lstrip("@")},
    ).mappings().first()
    if profile is None:
        return None

    stats_row = session.execute(
        text(
            """
            SELECT
                COUNT(*)                        AS posts_in_db,
                AVG(NULLIF(like_count, 0))      AS avg_likes,
                AVG(NULLIF(play_count, 0))      AS avg_play_count,
                AVG(score)                      AS avg_score,
                MAX(score)                      AS max_score,
                MAX(taken_at)                   AS last_post_at
            FROM ig_posts
            WHERE author_id = :author_id
            """
        ),
        {"author_id": profile["id"]},
    ).mappings().first()

    return {
        **dict(profile),
        "stats": {
            "posts_in_db": int(stats_row["posts_in_db"] or 0),
            "avg_likes": float(stats_row["avg_likes"]) if stats_row["avg_likes"] is not None else None,
            "avg_play_count": (
                float(stats_row["avg_play_count"]) if stats_row["avg_play_count"] is not None else None
            ),
            "avg_score": float(stats_row["avg_score"]) if stats_row["avg_score"] is not None else None,
            "max_score": float(stats_row["max_score"]) if stats_row["max_score"] is not None else None,
            "last_post_at": stats_row["last_post_at"],
        },
    }


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


def get_post_detail(
    session: Session, post_id: int, *, include_comments: int = 0
) -> Optional[Dict[str, Any]]:
    """Full single-post view: every column on `ig_posts`, the author's
    profile, the post's hashtags + mentions arrays, and optionally the
    top `include_comments` comments.

    Returns None when the post isn't in our DB. Heavy `raw` column is
    included intentionally — the detail view is for cases where the
    caller wants everything (AI pipeline, admin inspector). For lighter
    list shapes use `search_posts`.
    """
    row = session.execute(
        text(
            """
            SELECT
                p.id, p.code, p.media_type, p.product_type, p.taken_at,
                p.like_count, p.comment_count, p.play_count, p.view_count, p.save_count,
                p.video_duration, p.caption, p.caption_length,
                p.hashtags, p.mentions, p.language,
                p.emoji_count, p.hashtag_count, p.mention_count,
                p.has_question, p.has_cta, p.caption_simhash,
                p.thumbnail_url, p.media_urls, p.location, p.music_info,
                p.audio_track_id, p.score, p.score_components, p.score_computed_at,
                p.first_seen_at, p.last_seen_at, p.discovered_via_job_id,
                p.raw,
                u.id          AS author_id,
                u.username    AS author_username,
                u.full_name   AS author_full_name,
                u.biography   AS author_biography,
                u.follower_count, u.following_count, u.media_count,
                u.is_business, u.is_verified, u.is_private,
                u.profile_pic_url
            FROM ig_posts p
            JOIN ig_users u ON u.id = p.author_id
            WHERE p.id = :post_id
            LIMIT 1
            """
        ),
        {"post_id": int(post_id)},
    ).mappings().first()
    if row is None:
        return None
    result = dict(row)

    if include_comments > 0:
        result["comments"] = get_post_comments(session, int(post_id), limit=include_comments)
    else:
        result["comments"] = []
    return result


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
