"""MCP server for the ig_scraper service.

Exposes a curated set of read + write tools so AI agents (the future
content-generation pipeline in agent-platform, Claude Desktop, langgraph)
can consume our scraped data and trigger scans without going through
bespoke HTTP wrappers.

Design notes:
- We import FastMCP lazily — if the `mcp` package isn't installed (dev
  setups that don't need MCP), the API process still starts cleanly.
- Auth is the same `IG_SCRAPER_API_KEY` as the REST surface, sent as
  `Authorization: Bearer <key>` for streamable-HTTP transport.
- Tools are thin wrappers over `app.services.*` — no business logic
  lives in this module, only adapters. This way unit tests of the
  service layer cover the MCP surface for free.
- Account / proxy management is **deliberately not** exposed — too
  destructive to put behind an agent. REST-only.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.logging import logger


def _build_server():
    """Construct the FastMCP instance with every tool registered.

    Returns None when the `mcp` package isn't importable so callers can
    skip mounting cleanly. Production should always have it installed.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_unavailable", error=str(exc))
        return None

    mcp = FastMCP(name="ig-scraper", instructions=(
        "Read and trigger Instagram competitor scrapes. "
        "Use search_posts and get_user_top_posts to gather examples; "
        "use add_tracked_target to start daily scans of a username/hashtag."
    ))

    # ------------------------------------------------------------------
    # Read tools — primary surface for the AI generator
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_posts(
        author: Optional[str] = None,
        hashtag: Optional[str] = None,
        min_likes: Optional[int] = None,
        min_play_count: Optional[int] = None,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search scraped posts by author/hashtag/engagement.

        Returns up to `limit` posts (max 200), most recent first.
        Pass `since` as ISO 8601 (e.g. '2026-01-01T00:00:00Z').
        """
        from app.services.database import read_session_scope
        from app.services.queries import search_posts as _search

        since_dt = _parse_iso(since)
        with read_session_scope() as session:
            return _serialise(
                _search(
                    session,
                    author=author,
                    hashtag=hashtag,
                    min_likes=min_likes,
                    min_play_count=min_play_count,
                    since=since_dt,
                    limit=limit,
                )
            )

    @mcp.tool()
    def get_user_profile(username: str) -> Optional[Dict[str, Any]]:
        """Last known profile snapshot for `username` (without @)."""
        from app.services.database import read_session_scope
        from app.services.queries import get_user_profile as _get_profile

        with read_session_scope() as session:
            row = _get_profile(session, username)
        return _serialise_one(row) if row else None

    @mcp.tool()
    def get_user_top_posts(
        username: str,
        by: str = "likes",
        since: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Top posts for `username`, ordered by `likes`, `play_count`, `comments`, or `score`."""
        from app.services.database import read_session_scope
        from app.services.queries import get_user_top_posts as _top

        since_dt = _parse_iso(since)
        with read_session_scope() as session:
            return _serialise(
                _top(session, username=username, by=by, since=since_dt, limit=limit)
            )

    @mcp.tool()
    def get_high_scoring_posts(
        author: Optional[str] = None,
        hashtag: Optional[str] = None,
        since: Optional[str] = None,
        min_score: float = 60.0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Posts above `min_score` (0–100), ordered by score descending.

        This is the canonical "show me what's working" query for the
        AI content generator. Combine with `author` to study a single
        competitor or `hashtag` to scan a niche.
        """
        from app.services.database import read_session_scope
        from app.services.queries import get_user_top_posts as _top
        from app.services.queries import search_posts as _search

        since_dt = _parse_iso(since)
        with read_session_scope() as session:
            if author:
                rows = _top(
                    session, username=author, by="score", since=since_dt, limit=limit * 2
                )
            else:
                rows = _search(
                    session,
                    hashtag=hashtag,
                    since=since_dt,
                    limit=limit * 4,
                )
                rows = sorted(rows, key=lambda r: (r.get("score") or 0), reverse=True)
            rows = [r for r in rows if (r.get("score") or 0) >= min_score][:limit]
            return _serialise(rows)

    @mcp.tool()
    def get_post_comments(post_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        """Comments on a post, ordered by likes."""
        from app.services.database import read_session_scope
        from app.services.queries import get_post_comments as _comments

        with read_session_scope() as session:
            return _serialise(_comments(session, post_id=post_id, limit=limit))

    @mcp.tool()
    def get_recent_stories(
        username: str, since: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Recent stories captured for `username` (most recent first)."""
        from app.services.database import read_session_scope
        from app.services.queries import get_recent_stories as _stories

        since_dt = _parse_iso(since)
        with read_session_scope() as session:
            return _serialise(
                _stories(session, username=username, since=since_dt, limit=limit)
            )

    @mcp.tool()
    def list_tracked_targets(
        kind: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List currently tracked usernames and hashtags.

        `kind` ∈ {'user','hashtag'}; `status` ∈ {'active','paused','pending_review'}.
        """
        from app.services import targets as targets_service
        from app.services.database import read_session_scope

        with read_session_scope() as session:
            rows = targets_service.list_targets(session, kind=kind, status=status)
        return [_serialise_one(r.model_dump()) for r in rows]

    @mcp.tool()
    def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a job by id."""
        from app.services.database import read_session_scope
        from app.services.queries import get_job_status as _job

        with read_session_scope() as session:
            row = _job(session, job_id)
        return _serialise_one(row) if row else None

    # ------------------------------------------------------------------
    # Write tools — gated by the same API key on transport
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_tracked_target(
        kind: str,
        value: str,
        interval_hours: int = 24,
        fetch_stories: bool = True,
        fetch_highlights: bool = False,
        fetch_comments: bool = True,
        comment_limit: int = 50,
        min_likes: Optional[int] = None,
        min_impressions: Optional[int] = None,
        hashtag_section: str = "top",
    ) -> Dict[str, Any]:
        """Register a username or hashtag for recurring scans.

        First scan is a full backfill; subsequent runs are incremental
        (for users) or top/recent fetches (for hashtags).
        """
        from app.schemas.targets import TargetCreate
        from app.services import targets as targets_service
        from app.services.database import session_scope

        payload = TargetCreate(
            kind=kind,
            value=value,
            interval_hours=interval_hours,
            fetch_stories=fetch_stories,
            fetch_highlights=fetch_highlights,
            fetch_comments=fetch_comments,
            comment_limit=comment_limit,
            min_likes=min_likes,
            min_impressions=min_impressions,
            hashtag_section=hashtag_section,
        )
        with session_scope() as session:
            target = targets_service.create_target(session, payload)
        return _serialise_one(target.model_dump())

    @mcp.tool()
    def enqueue_user_scan(
        username: str,
        full_backfill: bool = False,
        fetch_comments: bool = True,
        comment_limit: int = 50,
        min_likes: Optional[int] = None,
        min_impressions: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Fire an ad-hoc scan of one username; returns the job id."""
        from app.schemas.jobs import JobCreate
        from app.services import jobs as jobs_service
        from app.services.database import session_scope

        job_type = "user_feed_full" if full_backfill else "user_feed_incremental"
        payload = JobCreate(
            job_type=job_type,
            target=username.lstrip("@").lower(),
            params={"fetch_comments": fetch_comments, "comment_limit": comment_limit},
            min_likes=min_likes,
            min_impressions=min_impressions,
        )
        with session_scope() as session:
            job = jobs_service.create_job(session, payload)
        return {"job_id": str(job.id), "status": job.status, "job_type": job.job_type}

    @mcp.tool()
    def enqueue_hashtag_scan(
        hashtag: str,
        section: str = "top",
        auto_enrich_users: bool = True,
        min_likes: Optional[int] = None,
        min_impressions: Optional[int] = None,
        max_posts: int = 100,
    ) -> Dict[str, Any]:
        """Fire an ad-hoc hashtag scan."""
        from app.schemas.jobs import JobCreate
        from app.services import jobs as jobs_service
        from app.services.database import session_scope

        if section not in {"top", "recent"}:
            raise ValueError("section must be 'top' or 'recent'")
        payload = JobCreate(
            job_type=f"hashtag_{section}",
            target=hashtag.lstrip("#").lower(),
            params={"auto_enrich_users": auto_enrich_users, "max_posts": max_posts},
            min_likes=min_likes,
            min_impressions=min_impressions,
        )
        with session_scope() as session:
            job = jobs_service.create_job(session, payload)
        return {"job_id": str(job.id), "status": job.status, "job_type": job.job_type}

    @mcp.tool()
    def activate_target(target_id: str) -> Dict[str, Any]:
        """Approve a `pending_review` target so the scheduler picks it up."""
        import uuid as _uuid

        from app.services import targets as targets_service
        from app.services.database import session_scope

        with session_scope() as session:
            row = targets_service.activate_target(session, _uuid.UUID(target_id))
        return _serialise_one(row.model_dump())

    @mcp.tool()
    def pause_target(target_id: str) -> Dict[str, Any]:
        """Pause a tracked target."""
        import uuid as _uuid

        from app.services import targets as targets_service
        from app.services.database import session_scope

        with session_scope() as session:
            row = targets_service.pause_target(session, _uuid.UUID(target_id))
        return _serialise_one(row.model_dump())

    return mcp


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _serialise_one(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce non-JSON types (UUID / datetime) to strings."""
    if row is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "hex") and not isinstance(value, (bytes, bytearray)):
            # uuid.UUID and similar
            out[key] = str(value)
        else:
            out[key] = value
    return out


def _serialise(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_serialise_one(r) for r in rows]


# Module-level singleton — safe to import without instantiating FastMCP
# more than once.
mcp_server = _build_server()
