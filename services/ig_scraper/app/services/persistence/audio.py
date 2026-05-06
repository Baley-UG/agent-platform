"""Upsert helper for ig_audio_tracks.

Pulls fields out of the `music_info` blob instagrapi attaches to reels
and clip media. Each track gets `use_count` incremented on every
sighting — useful later for trend detection.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlmodel import Session

_UPSERT = text(
    """
    INSERT INTO ig_audio_tracks (
        id, title, artist, original_audio_user_id, duration_ms,
        use_count, raw, first_seen_at, last_seen_at
    )
    VALUES (
        :id, :title, :artist, :original_audio_user_id, :duration_ms,
        1, CAST(:raw AS jsonb), :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        title                  = COALESCE(EXCLUDED.title, ig_audio_tracks.title),
        artist                 = COALESCE(EXCLUDED.artist, ig_audio_tracks.artist),
        original_audio_user_id = COALESCE(EXCLUDED.original_audio_user_id, ig_audio_tracks.original_audio_user_id),
        duration_ms            = COALESCE(EXCLUDED.duration_ms, ig_audio_tracks.duration_ms),
        use_count              = ig_audio_tracks.use_count + 1,
        raw                    = COALESCE(EXCLUDED.raw, ig_audio_tracks.raw),
        last_seen_at           = EXCLUDED.last_seen_at
    """
)


def _extract_track_id(music_info: Dict[str, Any]) -> Optional[str]:
    """instagrapi exposes audio under different shapes per media type.

    Try the common spots. Returns None when the media has no music
    component (regular feed posts).
    """
    if not music_info:
        return None
    metadata = music_info.get("music_info") or music_info
    canonical = metadata.get("music_canonical_id") or metadata.get("audio_cluster_id")
    if canonical:
        return str(canonical)
    asset = metadata.get("music_asset_info") or {}
    if asset.get("id"):
        return str(asset["id"])
    return None


def upsert_audio_track(
    session: Session, music_info: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Upsert from `music_info` payload. Returns the audio_track_id, or None."""
    if not music_info:
        return None
    track_id = _extract_track_id(music_info)
    if track_id is None:
        return None

    metadata = music_info.get("music_info") or music_info
    asset = metadata.get("music_asset_info") or {}
    consumption = metadata.get("music_consumption_info") or {}

    title = asset.get("title") or metadata.get("title")
    artist = asset.get("display_artist") or asset.get("artist")
    duration_ms = asset.get("duration_in_ms") or metadata.get("duration_in_ms")
    original_audio_user_id = consumption.get("original_sound_creator_user_id")

    session.execute(
        _UPSERT,
        {
            "id": track_id,
            "title": title,
            "artist": artist,
            "original_audio_user_id": (
                int(original_audio_user_id) if original_audio_user_id else None
            ),
            "duration_ms": int(duration_ms) if duration_ms else None,
            "raw": json.dumps(music_info, default=str),
            "now": datetime.now(timezone.utc),
        },
    )
    return track_id
