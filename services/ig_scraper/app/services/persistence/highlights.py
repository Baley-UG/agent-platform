"""Upsert helpers for ig_highlights + ig_highlight_items.

Highlight items are stories the owner chose to preserve past 24h, so
we still insert them through the stories pipeline — no special table.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlmodel import Session

_UPSERT_HIGHLIGHT = text(
    """
    INSERT INTO ig_highlights (
        id, owner_id, title, cover_url, media_count, raw, last_scanned_at, created_at
    )
    VALUES (
        :id, :owner_id, :title, :cover_url, :media_count,
        CAST(:raw AS jsonb), :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        title           = COALESCE(EXCLUDED.title, ig_highlights.title),
        cover_url       = COALESCE(EXCLUDED.cover_url, ig_highlights.cover_url),
        media_count     = COALESCE(EXCLUDED.media_count, ig_highlights.media_count),
        raw             = COALESCE(EXCLUDED.raw, ig_highlights.raw),
        last_scanned_at = EXCLUDED.last_scanned_at
    """
)

_INSERT_ITEM = text(
    """
    INSERT INTO ig_highlight_items (highlight_id, story_id, position)
    VALUES (:highlight_id, :story_id, :position)
    ON CONFLICT (highlight_id, story_id) DO NOTHING
    """
)


def upsert_highlight(
    session: Session,
    highlight: Dict[str, Any],
    *,
    owner_id: int,
) -> Optional[int]:
    """Upsert the highlight row. Returns the highlight pk (or None if missing)."""
    pk = int(highlight.get("pk") or highlight.get("id") or 0)
    if not pk:
        return None
    cover = highlight.get("cover_media") or {}
    cover_url = cover.get("cropped_image_version", {}).get("url") or cover.get("thumbnail_url")
    session.execute(
        _UPSERT_HIGHLIGHT,
        {
            "id": pk,
            "owner_id": owner_id,
            "title": highlight.get("title"),
            "cover_url": cover_url,
            "media_count": highlight.get("media_count"),
            "raw": json.dumps(highlight, default=str),
            "now": datetime.now(timezone.utc),
        },
    )
    return pk


def link_highlight_item(
    session: Session, highlight_id: int, story_id: int, position: int = 0
) -> None:
    """Wire a story into a highlight. Idempotent."""
    session.execute(
        _INSERT_ITEM,
        {"highlight_id": highlight_id, "story_id": story_id, "position": position},
    )
