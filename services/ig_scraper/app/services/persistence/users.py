"""Upsert helpers for ig_users.

Inputs are raw dicts (instagrapi pydantic models can be `.model_dump()`'d
before they reach this layer). Decoupling the persistence from the
instagrapi types keeps the scraper layer testable with simple fixtures.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlmodel import Session

_UPSERT = text(
    """
    INSERT INTO ig_users (
        id, username, full_name, biography,
        follower_count, following_count, media_count,
        is_business, is_verified, is_private, profile_pic_url,
        raw, first_seen_at, last_seen_at
    )
    VALUES (
        :id, :username, :full_name, :biography,
        :follower_count, :following_count, :media_count,
        :is_business, :is_verified, :is_private, :profile_pic_url,
        CAST(:raw AS jsonb), :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        username        = EXCLUDED.username,
        full_name       = COALESCE(EXCLUDED.full_name, ig_users.full_name),
        biography       = COALESCE(EXCLUDED.biography, ig_users.biography),
        follower_count  = COALESCE(EXCLUDED.follower_count, ig_users.follower_count),
        following_count = COALESCE(EXCLUDED.following_count, ig_users.following_count),
        media_count     = COALESCE(EXCLUDED.media_count, ig_users.media_count),
        is_business     = COALESCE(EXCLUDED.is_business, ig_users.is_business),
        is_verified     = COALESCE(EXCLUDED.is_verified, ig_users.is_verified),
        is_private      = COALESCE(EXCLUDED.is_private, ig_users.is_private),
        profile_pic_url = COALESCE(EXCLUDED.profile_pic_url, ig_users.profile_pic_url),
        raw             = COALESCE(EXCLUDED.raw, ig_users.raw),
        last_seen_at    = EXCLUDED.last_seen_at
    """
)


def upsert_ig_user(session: Session, user: Dict[str, Any]) -> int:
    """Upsert one IG user row. Returns the primary key.

    `user` should expose at least {id, username}; everything else is
    optional. The function defends against missing keys so it works for
    "thin" payloads (e.g. comment authors where we only have id+name).
    """
    import json

    user_id = int(user["id"])
    raw_payload: Optional[str] = None
    if "raw" in user and user["raw"] is not None:
        raw_payload = json.dumps(user["raw"], default=str)

    session.execute(
        _UPSERT,
        {
            "id": user_id,
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "biography": user.get("biography"),
            "follower_count": user.get("follower_count"),
            "following_count": user.get("following_count"),
            "media_count": user.get("media_count"),
            "is_business": user.get("is_business"),
            "is_verified": user.get("is_verified"),
            "is_private": user.get("is_private"),
            "profile_pic_url": user.get("profile_pic_url"),
            "raw": raw_payload,
            "now": datetime.now(timezone.utc),
        },
    )
    return user_id
