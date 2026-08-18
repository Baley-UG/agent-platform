"""Mirror scraped creatives into our own S3 bucket.

Why this exists at all: YouCloud CDN URLs are signed with
`auth_key=<epoch>-...` and stop resolving roughly 15 days after they are
issued. A creative we saw today is unrecoverable in two weeks unless we
took a copy — so unlike ig_scraper's `auto` policy, the default here is
`always`.

Policy (env `AD_MIRROR_MEDIA`):
    always → mirror every persisted material (default)
    job    → mirror only when the job was created with `mirror=true`
    never  → skip entirely (metadata-only ingestion)

Every transfer is best-effort. A failure logs, increments a counter and
leaves `media_s3_key` NULL; it never fails the ingestion job, because
losing one creative's bytes is much cheaper than losing the whole page of
metadata that came with it.

Downloads run in a worker thread. httpx's sync client is used on purpose:
these are large sequential body reads with a hard byte cap, and the async
client buys nothing while the thread is already off the event loop.
"""

from __future__ import annotations

import asyncio
import mimetypes
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx
from sqlalchemy import text as sql_text
from sqlmodel import Session

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_mirror_bytes_total, ad_mirror_failures_total
from app.services.parsing import filename_from_url

_PERSIST_KEYS = sql_text("""
    UPDATE ad_materials
       SET media_s3_key      = COALESCE(:media_key, media_s3_key),
           poster_s3_key     = COALESCE(:poster_key, poster_s3_key),
           media_mirrored_at = CASE
               WHEN :media_key IS NOT NULL OR :poster_key IS NOT NULL
               THEN :now ELSE media_mirrored_at END
     WHERE id = :material_id
    """)

_PERSIST_RESOURCE_KEY = sql_text("""
    UPDATE ad_material_resources
       SET s3_key = :s3_key
     WHERE material_id = :material_id AND idx = :idx
    """)


def should_mirror(*, job_mirror: Optional[bool]) -> bool:
    """Decide whether this material's bytes are worth fetching.

    `job_mirror` is tri-state: `None` means "follow the policy", `True`/`False`
    are the job's explicit intent.

        never  → off, whatever the job says. An operator who turns storage
                 off means it.
        always → on, unless the job explicitly opted out (default).
        job    → on only when the job explicitly opted in.

    An explicit `mirror=False` used to be silently ignored under `always`,
    which is the same failure shape as a filter that returns zero rows
    without erroring: the request was accepted and quietly did the opposite.
    An operator's per-job decision now wins over the policy default.
    """
    mode = (settings.AD_MIRROR_MEDIA or "always").lower()
    if mode == "never":
        return False
    if mode == "job":
        return job_mirror is True
    # `always` — and anything unrecognised, because the whole point of the
    # service is that the source URLs expire. Failing open on an env typo is
    # safer than silently skipping mirrors.
    if mode != "always":
        logger.warning("ad_mirror_policy_unknown", policy=mode, note="treating as 'always'")
    return job_mirror is not False


def _download(url: str) -> Optional[Tuple[bytes, str]]:
    """GET `url`, capped at `AD_MIRROR_MAX_BYTES`.

    Returns `(bytes, content_type)`, or None on any failure — HTTP error,
    timeout, or a body that exceeds the cap. Streams so an oversized file
    is abandoned mid-transfer instead of being buffered in full first.

    Measured against the live CDN:
      * `referer` is NOT validated — a deliberately wrong one still gets
        200, so we don't pretend to be the web app.
      * the `auth_key` signature IS enforced (tampering it gives 401),
        which is the whole reason this mirror exists.
      * the CDN content-negotiates on `Accept`. A browser's
        `image/avif,image/webp,...` gets a 61 KB **webp**; no Accept header
        gets the original 156 KB **jpeg**. We ask for the original on
        purpose — `ad_materials.media_format` comes from the API payload
        ("jpeg"), so accepting a webp substitution would leave the column
        describing bytes we don't actually have.
    """
    headers = {
        # Pinned, not browser-mimicking: see the note above about webp
        # substitution silently contradicting `media_format`.
        "accept": "video/mp4,image/jpeg,image/png,*/*;q=0.5",
        "user-agent": settings.AD_API_USER_AGENT,
    }
    try:
        with httpx.Client(
            timeout=settings.AD_MIRROR_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=headers,
            trust_env=False,
        ) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    # 401/403 here almost always means the auth_key expired
                    # or was mangled — the bytes are gone for good.
                    reason = (
                        "expired_or_forbidden"
                        if response.status_code in (401, 403)
                        else f"http_{response.status_code}"
                    )
                    ad_mirror_failures_total.labels(reason=reason).inc()
                    logger.warning("ad_mirror_http_error", status=response.status_code, url=url[:120])
                    return None
                content_type = response.headers.get("content-type", "application/octet-stream")
                content_type = content_type.split(";")[0].strip()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > settings.AD_MIRROR_MAX_BYTES:
                        ad_mirror_failures_total.labels(reason="too_large").inc()
                        logger.warning(
                            "ad_mirror_too_large",
                            url=url[:120],
                            size=len(buffer),
                            cap=settings.AD_MIRROR_MAX_BYTES,
                        )
                        return None
        return bytes(buffer), content_type
    except httpx.HTTPError as exc:
        ad_mirror_failures_total.labels(reason="transport").inc()
        logger.warning("ad_mirror_fetch_failed", url=url[:120], error=str(exc))
        return None


def _upload(material_id: str, url: str, kind: str) -> Optional[str]:
    """Download one URL and put it in S3. Returns the key, or None."""
    got = _download(url)
    if got is None:
        return None
    data, content_type = got

    fallback_ext = ".mp4" if kind == "media" else ".jpg"
    ext = mimetypes.guess_extension(content_type) or fallback_ext
    key = s3lib.make_material_key(material_id, filename_from_url(url, ext))
    try:
        s3lib.upload_bytes(key, data, content_type=content_type)
    except Exception as exc:  # noqa: BLE001 — boto3 raises a wide family
        ad_mirror_failures_total.labels(reason="upload").inc()
        logger.warning("ad_mirror_upload_failed", key=key, error=str(exc))
        return None

    ad_mirror_bytes_total.labels(kind=kind).inc(len(data))
    # `content_type` is logged so a CDN-side format switch (see `_download`'s
    # note on Accept negotiation) shows up in Loki instead of quietly
    # diverging from `ad_materials.media_format`.
    logger.info(
        "ad_mirror_uploaded",
        material_id=material_id,
        kind=kind,
        key=key,
        bytes=len(data),
        content_type=content_type,
    )
    return key


def transfer(
    *,
    material_id: str,
    media_url: Optional[str],
    poster_url: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Blocking download + upload. Returns `(media_key, poster_key)`.

    Touches no database — see `persist_keys` for that half. Keeping the
    two apart is what lets the transfer run in a worker thread: a
    SQLAlchemy Session must not be handed across threads, so the network
    work goes off-loop and the write stays on it.
    """
    if not s3lib.is_configured():
        logger.info("ad_mirror_skipped_unconfigured", material_id=material_id)
        return None, None
    if not media_url and not poster_url:
        return None, None

    media_key = _upload(material_id, media_url, "media") if media_url else None
    poster_key: Optional[str] = None

    # An image creative's "media" IS its poster — no second fetch needed.
    if media_key and media_url and _looks_like_image(media_url, media_key):
        poster_key = media_key
    elif poster_url:
        poster_key = _upload(material_id, poster_url, "poster")

    return media_key, poster_key


def persist_keys(
    session: Session,
    *,
    material_id: str,
    media_key: Optional[str],
    poster_key: Optional[str],
) -> None:
    """Stamp the material row with whichever mirror keys succeeded."""
    if not (media_key or poster_key):
        return
    session.execute(
        _PERSIST_KEYS,
        {
            "material_id": material_id,
            "media_key": media_key,
            "poster_key": poster_key,
            "now": datetime.now(timezone.utc),
        },
    )
    if media_key:
        # The denormalised primary resource is index 0 by construction
        # (see persistence.materials.extract_resources).
        session.execute(
            _PERSIST_RESOURCE_KEY,
            {"material_id": material_id, "idx": 0, "s3_key": media_key},
        )


def _looks_like_image(url: str, key: str) -> bool:
    """True when the primary asset is itself an image."""
    for candidate in (key, url.split("?", 1)[0]):
        lowered = candidate.lower()
        if lowered.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return True
    return False


async def transfer_async(
    *,
    material_id: str,
    media_url: Optional[str],
    poster_url: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Run `transfer` off the event loop. Still no database access."""
    return await asyncio.to_thread(
        transfer,
        material_id=material_id,
        media_url=media_url,
        poster_url=poster_url,
    )
