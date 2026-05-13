"""Mirror policy for scraped Instagram media.

Why we mirror: IG CDN URLs are signed and expire (typically 1-7 days).
Posts we want to revisit later — tracked targets, manually-pinned items,
admin-imported references — need a copy in our own S3.

Policy (env `IG_MIRROR_MEDIA`):
    - `always`  → mirror every persisted post
    - `auto`    → mirror only when the post is "important" (tracked
                  author, or explicitly pinned). Default.
    - `never`   → skip entirely (legacy behaviour pre-mirror)

The actual transfer is best-effort: a failure logs a warning but never
breaks the scrape transaction. Callers re-read the row to confirm
`media_s3_key` was set.

Re-fetch path (lazy): when the panel asks for a post whose mirror is
missing and the cached IG URL has died, we re-hit HikerAPI's media
endpoint (`/v1/media/by/id`) for a fresh signed URL, then mirror that.
This costs 1 HikerAPI credit per re-fetch — gated behind an explicit
admin action, not implicit polling.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx
from sqlalchemy import text as sql_text
from sqlmodel import Session

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger


def _filename_from_url(url: str, fallback_ext: str = ".bin") -> str:
    try:
        name = os.path.basename(urlparse(url).path)
    except Exception:  # noqa: BLE001
        name = ""
    if not name:
        name = f"asset{fallback_ext}"
    if "." not in name:
        name = f"{name}{fallback_ext}"
    return name[:80]


def _download(url: str) -> Optional[Tuple[bytes, str]]:
    """GET `url`, capped at `IG_MIRROR_MAX_BYTES`. Returns `(bytes, content_type)`
    or `None` on any error (HTTP 4xx/5xx, timeout, size-cap exceeded)."""
    try:
        with httpx.Client(timeout=settings.IG_MIRROR_TIMEOUT_SECONDS, follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    logger.warning("ig_mirror_http_error", status=r.status_code, url=url[:120])
                    return None
                ct = r.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > settings.IG_MIRROR_MAX_BYTES:
                        logger.warning("ig_mirror_too_large", url=url[:120], size=len(buf))
                        return None
        return bytes(buf), ct
    except httpx.HTTPError as exc:
        logger.warning("ig_mirror_fetch_failed", url=url[:120], error=str(exc))
        return None


def is_tracked_author(session: Session, author_id: Optional[int]) -> bool:
    """Return True when this user is an active tracked target.

    Match by `ig_users.username = ig_scan_targets.value` (kind='username',
    status='active'). Catches posts scraped via hashtag jobs whose
    author happens to be one we follow.
    """
    if not author_id:
        return False
    row = session.execute(
        sql_text(
            """
            SELECT 1
            FROM ig_users u
            JOIN ig_scan_targets t
              ON t.value = u.username
             AND t.kind = 'username'
             AND t.status = 'active'
            WHERE u.id = :uid
            LIMIT 1
            """
        ),
        {"uid": int(author_id)},
    ).first()
    return row is not None


def should_mirror(session: Session, *, author_id: Optional[int]) -> bool:
    """Decide whether a post deserves to be mirrored at scrape time."""
    mode = (settings.IG_MIRROR_MEDIA or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    # auto
    return is_tracked_author(session, author_id)


def _persist_keys(
    session: Session, post_id: int, media_key: Optional[str], poster_key: Optional[str]
) -> None:
    """Stamp the post row with the mirror outcome."""
    session.execute(
        sql_text(
            """
            UPDATE ig_posts
               SET media_s3_key      = COALESCE(:media_key, media_s3_key),
                   poster_s3_key     = COALESCE(:poster_key, poster_s3_key),
                   media_mirrored_at = CASE
                       WHEN :media_key IS NOT NULL OR :poster_key IS NOT NULL
                       THEN :now ELSE media_mirrored_at END
             WHERE id = :pid
            """
        ),
        {
            "pid": int(post_id),
            "media_key": media_key,
            "poster_key": poster_key,
            "now": datetime.now(timezone.utc),
        },
    )


def mirror_post_media(
    session: Session,
    *,
    post_id: int,
    media_urls: Optional[list],
    thumbnail_url: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Download + S3 + DB update. Returns `(media_key, poster_key)`.

    Best-effort: any failure logs and returns the keys that did succeed
    (often `(None, None)`). Caller decides whether to surface to the
    admin (e.g. `mirror_pending=True`).
    """
    if not s3lib.is_configured():
        logger.info("ig_mirror_skipped_unconfigured", post_id=post_id)
        return None, None

    primary = media_urls[0] if isinstance(media_urls, list) and media_urls else None
    if not primary and not thumbnail_url:
        return None, None

    media_key: Optional[str] = None
    poster_key: Optional[str] = None

    if primary:
        got = _download(primary)
        if got:
            data, ct = got
            ext = mimetypes.guess_extension(ct) or ".bin"
            key = s3lib.make_post_key(post_id, _filename_from_url(primary, ext))
            try:
                s3lib.upload_bytes(key, data, content_type=ct)
                media_key = key
                # If the primary IS an image, reuse it as the poster.
                if ct.startswith("image/"):
                    poster_key = key
            except Exception as exc:  # noqa: BLE001
                logger.warning("ig_mirror_upload_failed", key=key, error=str(exc))

    if poster_key is None and thumbnail_url:
        got = _download(thumbnail_url)
        if got:
            data, ct = got
            ext = mimetypes.guess_extension(ct) or ".jpg"
            key = s3lib.make_post_key(post_id, _filename_from_url(thumbnail_url, ext))
            try:
                s3lib.upload_bytes(key, data, content_type=ct)
                poster_key = key
            except Exception as exc:  # noqa: BLE001
                logger.warning("ig_mirror_poster_upload_failed", key=key, error=str(exc))

    if media_key or poster_key:
        _persist_keys(session, post_id, media_key, poster_key)

    return media_key, poster_key


async def refetch_via_hikerapi(post_id: int) -> Optional[Dict[str, Any]]:
    """Hit HikerAPI's media-by-id endpoint for a fresh signed URL set.

    Returns the raw media payload (same shape as scrape time) or None.
    Each call costs 1 HikerAPI credit — only invoke on explicit admin
    action (e.g. clicking "re-fetch" on a dead post).
    """
    from app.services.scrapers.hikerapi.client import HikerAPIClient, HikerAPIError

    try:
        async with HikerAPIClient() as client:
            payload = await client.get("/v1/media/by/id", id=int(post_id))
    except HikerAPIError as exc:
        logger.warning("ig_refetch_hikerapi_failed", post_id=post_id, error=str(exc))
        return None

    # HikerAPI usually returns `[media_dict, meta]` for chunked endpoints
    # but media/by/id returns the raw dict directly. Tolerate both.
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return None
