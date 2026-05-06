"""Upsert helper for ig_posts (+ ig_post_metric_snapshots + ig_post_hashtags).

This is the load-bearing scraper write path. One call:
- upserts the post row;
- writes a fresh `ig_post_metric_snapshots` row;
- ensures every hashtag exists in `ig_hashtags`;
- inserts the (post, hashtag) edges if they're new;
- enriches caption columns from `app.services.features`.

Audio normalisation lives in `persistence.audio`; the caller is
responsible for upserting audio first and passing the track_id in.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlmodel import Session

from app.services.features import extract as extract_features
from app.services.persistence.hashtags import upsert_hashtag

_UPSERT_POST = text(
    """
    INSERT INTO ig_posts (
        id, code, media_type, product_type, author_id,
        caption, taken_at,
        like_count, comment_count, play_count, view_count, save_count,
        video_duration, thumbnail_url, media_urls, location, music_info,
        audio_track_id, hashtags, mentions,
        language, emoji_count, hashtag_count, mention_count, caption_length,
        has_question, has_cta, caption_simhash,
        raw, discovered_via_job_id, first_seen_at, last_seen_at
    )
    VALUES (
        :id, :code, :media_type, :product_type, :author_id,
        :caption, :taken_at,
        :like_count, :comment_count, :play_count, :view_count, :save_count,
        :video_duration, :thumbnail_url,
        CAST(:media_urls AS jsonb), CAST(:location AS jsonb), CAST(:music_info AS jsonb),
        :audio_track_id, CAST(:hashtags AS text[]), CAST(:mentions AS text[]),
        :language, :emoji_count, :hashtag_count, :mention_count, :caption_length,
        :has_question, :has_cta, :caption_simhash,
        CAST(:raw AS jsonb), :job_id, :now, :now
    )
    ON CONFLICT (id) DO UPDATE SET
        like_count       = EXCLUDED.like_count,
        comment_count    = EXCLUDED.comment_count,
        play_count       = COALESCE(EXCLUDED.play_count, ig_posts.play_count),
        view_count       = COALESCE(EXCLUDED.view_count, ig_posts.view_count),
        save_count       = COALESCE(EXCLUDED.save_count, ig_posts.save_count),
        thumbnail_url    = COALESCE(EXCLUDED.thumbnail_url, ig_posts.thumbnail_url),
        media_urls       = COALESCE(EXCLUDED.media_urls, ig_posts.media_urls),
        audio_track_id   = COALESCE(EXCLUDED.audio_track_id, ig_posts.audio_track_id),
        raw              = COALESCE(EXCLUDED.raw, ig_posts.raw),
        last_seen_at     = EXCLUDED.last_seen_at
    """
)

_INSERT_SNAPSHOT = text(
    """
    INSERT INTO ig_post_metric_snapshots (
        post_id, scanned_at, like_count, comment_count,
        play_count, view_count, save_count, score
    )
    VALUES (
        :post_id, :now, :like_count, :comment_count,
        :play_count, :view_count, :save_count, NULL
    )
    ON CONFLICT (post_id, scanned_at) DO NOTHING
    """
)

_INSERT_POST_HASHTAG = text(
    """
    INSERT INTO ig_post_hashtags (post_id, hashtag)
    VALUES (:post_id, :hashtag)
    ON CONFLICT DO NOTHING
    """
)


def upsert_post(
    session: Session,
    *,
    media: Dict[str, Any],
    author_id: int,
    job_id: Optional[uuid.UUID],
    audio_track_id: Optional[str] = None,
) -> int:
    """Upsert one post + write its metric snapshot + link hashtags.

    `media` is a raw dict (instagrapi `Media.model_dump()` works). The
    function reads only the keys it needs and ignores the rest, which
    keeps it tolerant to instagrapi version drift.
    """
    now = datetime.now(timezone.utc)
    caption_text = media.get("caption_text") or media.get("caption")
    features = extract_features(caption_text)

    media_type = int(media.get("media_type") or 0)
    product_type = media.get("product_type")
    code = media.get("code")
    taken_at = media.get("taken_at")
    if isinstance(taken_at, (int, float)):
        taken_at = datetime.fromtimestamp(taken_at, tz=timezone.utc)
    if not isinstance(taken_at, datetime):
        taken_at = now

    media_urls = _collect_media_urls(media)
    location = media.get("location")
    music_info = media.get("music_info") or media.get("clips_metadata")

    post_id = int(media["pk"] if "pk" in media else media["id"])
    params = {
        "id": post_id,
        "code": code,
        "media_type": media_type,
        "product_type": product_type,
        "author_id": author_id,
        "caption": caption_text,
        "taken_at": taken_at,
        "like_count": int(media.get("like_count") or 0),
        "comment_count": int(media.get("comment_count") or 0),
        "play_count": _maybe_int(media.get("play_count") or media.get("video_view_count")),
        "view_count": _maybe_int(media.get("view_count")),
        "save_count": _maybe_int(media.get("save_count")),
        "video_duration": _maybe_float(media.get("video_duration")),
        "thumbnail_url": media.get("thumbnail_url"),
        "media_urls": json.dumps(media_urls, default=str) if media_urls else None,
        "location": json.dumps(location, default=str) if location else None,
        "music_info": json.dumps(music_info, default=str) if music_info else None,
        "audio_track_id": audio_track_id,
        "hashtags": features.hashtags or None,
        "mentions": features.mentions or None,
        "language": features.language,
        "emoji_count": features.emoji_count,
        "hashtag_count": features.hashtag_count,
        "mention_count": features.mention_count,
        "caption_length": features.caption_length,
        "has_question": features.has_question,
        "has_cta": features.has_cta,
        "caption_simhash": features.caption_simhash_signed,
        "raw": json.dumps(media, default=str),
        "job_id": job_id,
        "now": now,
    }
    session.execute(_UPSERT_POST, params)

    session.execute(
        _INSERT_SNAPSHOT,
        {
            "post_id": post_id,
            "now": now,
            "like_count": params["like_count"],
            "comment_count": params["comment_count"],
            "play_count": params["play_count"],
            "view_count": params["view_count"],
            "save_count": params["save_count"],
        },
    )

    for tag in features.hashtags:
        upsert_hashtag(session, tag)
        session.execute(_INSERT_POST_HASHTAG, {"post_id": post_id, "hashtag": tag})

    return post_id


def _collect_media_urls(media: Dict[str, Any]) -> List[str]:
    """Walk the media payload and pull every URL-shaped field worth keeping.

    Carousels expose children under `resources`; videos expose
    `video_url`; images expose `thumbnail_url` and `image_versions2`.
    Order is best-effort, not guaranteed.
    """
    urls: List[str] = []

    def _push(value: Any) -> None:
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)

    _push(media.get("video_url"))
    _push(media.get("thumbnail_url"))
    _push(media.get("image_url"))

    image_versions = media.get("image_versions2") or {}
    for candidate in (image_versions.get("candidates") or []):
        _push(candidate.get("url"))

    for resource in (media.get("resources") or []):
        _push(resource.get("video_url"))
        _push(resource.get("thumbnail_url"))
        for candidate in ((resource.get("image_versions2") or {}).get("candidates") or []):
            _push(candidate.get("url"))

    # De-duplicate while preserving order.
    return list(dict.fromkeys(urls))


def _maybe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
