"""Insert-only persistence for ig_stories.

Stories are ephemeral (24h on IG) — once expired we never see them
again, so we capture them once and never update. The single SQL
statement uses `ON CONFLICT DO NOTHING` so a re-run within the same
24h window is a no-op rather than an error.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlmodel import Session

_INSERT = text(
    """
    INSERT INTO ig_stories (
        id, author_id, media_type, taken_at, expires_at,
        video_duration, media_url, thumbnail_url, caption,
        mentions, hashtags, link_sticker_url, seen_count,
        raw, discovered_via_job_id, captured_at
    )
    VALUES (
        :id, :author_id, :media_type, :taken_at, :expires_at,
        :video_duration, :media_url, :thumbnail_url, :caption,
        CAST(:mentions AS text[]), CAST(:hashtags AS text[]),
        :link_sticker_url, :seen_count,
        CAST(:raw AS jsonb), :job_id, :now
    )
    ON CONFLICT (id) DO NOTHING
    """
)


def _coerce_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _extract_sticker_meta(story: Dict[str, Any]) -> tuple[List[str], List[str], Optional[str]]:
    """Walk stickers for hashtags / mentions / swipe-up link.

    instagrapi attaches them under different keys depending on version;
    walk all the obvious spots and dedupe. Best-effort — any miss just
    means an empty list.
    """
    hashtags: List[str] = []
    mentions: List[str] = []
    link_url: Optional[str] = None

    for source in (story.get("hashtags") or []):
        name = source.get("name") if isinstance(source, dict) else source
        if name:
            hashtags.append(str(name).lower().lstrip("#"))
    for source in (story.get("mentions") or []):
        username = source.get("username") if isinstance(source, dict) else source
        if username:
            mentions.append(str(username).lower().lstrip("@"))

    for link in (story.get("links") or []):
        url = link.get("webUri") or link.get("url") if isinstance(link, dict) else None
        if url:
            link_url = url
            break

    return list(dict.fromkeys(hashtags)), list(dict.fromkeys(mentions)), link_url


def insert_story(
    session: Session,
    story: Dict[str, Any],
    *,
    author_id: int,
    job_id: Optional[uuid.UUID],
) -> bool:
    """Insert one story row. Returns True if it landed (False = duplicate).

    Stories are insert-only; we never update an existing row.
    """
    story_pk = int(story.get("pk") or story.get("id") or 0)
    if not story_pk:
        return False

    taken_at = _coerce_dt(story.get("taken_at")) or datetime.now(timezone.utc)
    expires_at = (
        _coerce_dt(story.get("expiring_at"))
        or _coerce_dt(story.get("expires_at"))
        or (taken_at + timedelta(hours=24))
    )
    hashtags, mentions, link_url = _extract_sticker_meta(story)

    session.execute(
        _INSERT,
        {
            "id": story_pk,
            "author_id": author_id,
            "media_type": int(story.get("media_type") or 1),
            "taken_at": taken_at,
            "expires_at": expires_at,
            "video_duration": story.get("video_duration"),
            "media_url": story.get("video_url") or story.get("thumbnail_url"),
            "thumbnail_url": story.get("thumbnail_url"),
            "caption": story.get("caption_text") or story.get("caption"),
            "mentions": mentions or None,
            "hashtags": hashtags or None,
            "link_sticker_url": link_url,
            "seen_count": story.get("seen") or story.get("seen_count"),
            "raw": json.dumps(story, default=str),
            "job_id": job_id,
            "now": datetime.now(timezone.utc),
        },
    )
    return True
