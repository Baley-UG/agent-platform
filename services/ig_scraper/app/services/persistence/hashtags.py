"""Upsert helper for ig_hashtags."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session

_UPSERT = text(
    """
    INSERT INTO ig_hashtags (name, media_count, last_scanned_at)
    VALUES (:name, :media_count, :now)
    ON CONFLICT (name) DO UPDATE SET
        media_count     = COALESCE(EXCLUDED.media_count, ig_hashtags.media_count),
        last_scanned_at = EXCLUDED.last_scanned_at
    """
)


def upsert_hashtag(session: Session, name: str, media_count: Optional[int] = None) -> str:
    """Upsert a hashtag row, normalising to lowercase + no leading `#`."""
    normalised = name.lstrip("#").lower()
    session.execute(
        _UPSERT,
        {
            "name": normalised,
            "media_count": media_count,
            "now": datetime.now(timezone.utc),
        },
    )
    return normalised
