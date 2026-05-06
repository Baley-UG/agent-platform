"""Author enrichment for hashtag scans.

When a hashtag scrape surfaces a post by an author we don't yet know
much about, we want to (a) fill in their full profile and (b) decide
whether to start tracking them daily.

Plan § 6.6 calls for three gates:
    - follower_count >= IG_MIN_FOLLOWERS_FOR_ENRICH
    - media_count    >= IG_MIN_MEDIA_FOR_ENRICH
    - is_private == False

If `IG_AUTO_PROMOTE_DISCOVERED=true` the new target lands as
`status='active'` and the scheduler picks it up on its next tick;
otherwise it sits in `status='pending_review'` until a human approves.

The follower / media-count cap implementation lives here, but the
**median-score** gate (§ 6.6 closing paragraph) only fires once M8 has
populated `ig_posts.score`. For now `_should_promote` checks the
deterministic thresholds; the score check will be added in M8 without
changing this module's public surface.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.logging import logger
from app.services.database import session_scope
from app.services.persistence import upsert_ig_user

ENRICH_REFRESH_DAYS = 7


async def _fetch_user_info(client, user_id: int) -> Dict[str, Any]:
    info = await asyncio.to_thread(client.user_info_v1, user_id)
    if hasattr(info, "model_dump"):
        return info.model_dump(mode="python")
    if hasattr(info, "dict"):
        return info.dict()
    return dict(vars(info))


def _is_stale(session: Session, user_id: int) -> bool:
    """True when `last_seen_at` is missing or older than the refresh window."""
    row = session.execute(
        text("SELECT last_seen_at FROM ig_users WHERE id = :id"),
        {"id": user_id},
    ).first()
    if row is None or row[0] is None:
        return True
    threshold = datetime.now(timezone.utc) - timedelta(days=ENRICH_REFRESH_DAYS)
    last_seen = row[0]
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return last_seen < threshold


def _existing_target_id(session: Session, username: str) -> Optional[uuid.UUID]:
    row = session.execute(
        text("SELECT id FROM ig_scan_targets WHERE kind = 'user' AND value = :v"),
        {"v": username.lower()},
    ).first()
    return row[0] if row else None


def _should_promote(profile: Dict[str, Any], *, min_followers: int, min_media: int) -> bool:
    if profile.get("is_private"):
        return False
    if int(profile.get("follower_count") or 0) < min_followers:
        return False
    if int(profile.get("media_count") or 0) < min_media:
        return False
    return True


_INSERT_TARGET = text(
    """
    INSERT INTO ig_scan_targets (
        id, kind, value, status, interval_hours,
        fetch_feed, fetch_stories, fetch_highlights, fetch_comments,
        comment_limit, hashtag_section, first_backfill_done,
        next_run_at, auto_discovered, source_target_id,
        created_at, updated_at
    )
    VALUES (
        gen_random_uuid(), 'user', :value, :status, :interval_hours,
        TRUE, TRUE, FALSE, TRUE,
        :comment_limit, 'top', FALSE,
        :now, TRUE, :source_target_id,
        :now, :now
    )
    ON CONFLICT (kind, value) DO NOTHING
    RETURNING id
    """
)


async def enrich_authors(
    client,
    throttle,
    *,
    authors: Dict[int, Dict[str, Any]],
    source_target_id: Optional[uuid.UUID],
    min_followers: int,
    min_media: int,
) -> Dict[str, int]:
    """Refresh stale author profiles and auto-promote those that qualify.

    Returns aggregate stats: `users_enriched`, `targets_created`.

    `client` is a logged-in instagrapi Client; `throttle` is the
    caller's Throttle so the per-action delay budget stays consistent
    across the hashtag scrape and the enrichment pass.
    """
    users_enriched = 0
    targets_created = 0

    promote_status = (
        "active" if settings.IG_AUTO_PROMOTE_DISCOVERED else "pending_review"
    )

    for user_id, payload in authors.items():
        username = (payload.get("username") or "").lower()
        if not username:
            continue

        # Decide whether we need a fresh user_info call.
        with session_scope() as session:
            already_target = _existing_target_id(session, username) is not None
            stale = _is_stale(session, user_id)

        if already_target:
            # Nothing to do — daily scheduler already handles them.
            continue

        if stale:
            try:
                profile = await _fetch_user_info(client, user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "enrichment_user_info_failed",
                    user_id=user_id,
                    error=str(exc),
                )
                continue
            users_enriched += 1
            with session_scope() as session:
                upsert_ig_user(
                    session,
                    {**profile, "id": user_id, "raw": profile},
                )
            await throttle.after_action("profile")
        else:
            # Use whatever payload the hashtag scrape already gave us.
            profile = payload

        if not _should_promote(profile, min_followers=min_followers, min_media=min_media):
            continue

        with session_scope() as session:
            row = session.execute(
                _INSERT_TARGET,
                {
                    "value": username,
                    "status": promote_status,
                    "interval_hours": settings.IG_DEFAULT_INTERVAL_HOURS,
                    "comment_limit": settings.IG_COMMENT_DEFAULT_LIMIT,
                    "source_target_id": source_target_id,
                    "now": datetime.now(timezone.utc),
                },
            ).first()
            if row is not None:
                targets_created += 1
                logger.info(
                    "auto_target_created",
                    username=username,
                    status=promote_status,
                    follower_count=profile.get("follower_count"),
                    media_count=profile.get("media_count"),
                    source_target_id=str(source_target_id) if source_target_id else None,
                )

    return {"users_enriched": users_enriched, "targets_created": targets_created}
