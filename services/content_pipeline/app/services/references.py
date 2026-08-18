"""content_references CRUD + ingestion helpers."""

from __future__ import annotations

import mimetypes
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core import s3
from app.core.config import settings
from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.projects import Project
from app.models.reference_usages import ReferenceUsage
from app.schemas.references import (
    ReferenceImportFromAds,
    ReferenceImportFromScraper,
    ReferenceManualUpload,
    ReferenceRead,
    ReferenceUpdate,
    UsageCheck,
)
from app.services import ad_scraper_bridge, scraper_bridge


def to_read(
    ref: ContentReference, *, session: Optional[Session] = None
) -> ReferenceRead:
    """Convert an ORM `ContentReference` to a `ReferenceRead` with
    ready-to-use presigned URLs + scenario count baked in.

    `session` is optional ONLY because some legacy callers (tests) build
    a reference without DB context. When provided, we COUNT how many
    scenarios target this reference so the panel can show a "has
    scenario" badge without a second round trip.
    """
    # Default TTL keeps signing cheap (no extra S3 call) and matches
    # the `/preview-url` endpoint's default.
    ttl = settings.S3_PRESIGNED_URL_TTL_SECONDS
    media_url: Optional[str] = None
    poster_url: Optional[str] = None
    if s3.is_configured():
        try:
            if ref.media_s3_key:
                media_url = s3.presigned_get_url(ref.media_s3_key, ttl=ttl)
            if ref.poster_s3_key:
                poster_url = s3.presigned_get_url(ref.poster_s3_key, ttl=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reference_presign_failed", reference_id=str(ref.id), error=str(exc))
    # Fall back to IG CDN URLs stored in metadata at import time.
    meta = ref.metadata_json or {}
    if media_url is None:
        ig_urls = meta.get("ig_media_urls") or []
        if isinstance(ig_urls, list) and ig_urls:
            media_url = ig_urls[0]
    if poster_url is None:
        poster_url = meta.get("ig_thumbnail_url") or media_url

    # Scenario count — single COUNT(*) keyed on reference_id. Skip when
    # no session was supplied (older test paths).
    scenarios_count = 0
    if session is not None:
        from sqlalchemy import func
        from app.models.scenarios import Scenario

        scenarios_count = int(
            session.exec(
                select(func.count(Scenario.id)).where(Scenario.reference_id == ref.id)
            ).one()
            or 0
        )

    payload = ReferenceRead.model_validate(ref)
    payload.media_url = media_url
    payload.poster_url = poster_url
    payload.scenarios_count = scenarios_count
    return payload


# Cap mirrored bytes per source so a malicious / oversized CDN URL can't
# stuff our bucket. 50 MB covers 4K video posters and short reels.
_MIRROR_MAX_BYTES = 50 * 1024 * 1024
_MIRROR_TIMEOUT_SECONDS = 30.0


def _filename_from_url(url: str, fallback_ext: str = ".jpg") -> str:
    """Pull a usable filename from an IG CDN URL.

    IG CDN paths look like `.../687789371_..._n.jpg?stp=...`. We strip
    the query string, take the basename, and fall back to a UUID when
    the URL doesn't carry one. The result is only used inside the
    canonical S3 key (`make_key` adds its own UUID prefix) so collisions
    don't matter much.
    """
    try:
        path = urlparse(url).path
        name = os.path.basename(path) or f"asset{fallback_ext}"
    except Exception:  # noqa: BLE001
        name = f"asset{fallback_ext}"
    if "." not in name:
        name = f"{name}{fallback_ext}"
    return name[:100]


def _mirror_to_s3(
    url: str, project_id: uuid.UUID, kind: str = "references"
) -> Optional[Tuple[str, str]]:
    """Download `url` and stash it in our S3 bucket.

    Returns `(s3_key, content_type)` on success, `None` on any error.
    Errors are intentionally non-fatal — the reference row gets created
    either way, and the admin panel can either re-trigger a mirror or
    fall back to the original CDN URL stored in `metadata_json`.

    IG CDN URLs carry short-lived signatures; we fetch immediately at
    import time. If the import is delayed for hours and the URL has
    expired, the request returns a 403 / 410 and we leave `media_s3_key`
    null.
    """
    if not url:
        return None
    try:
        with httpx.Client(timeout=_MIRROR_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning(
                        "reference_mirror_http_error",
                        status=resp.status_code,
                        url=url[:120],
                    )
                    return None
                content_type = resp.headers.get("content-type", "application/octet-stream").split(";")[0]
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _MIRROR_MAX_BYTES:
                        logger.warning(
                            "reference_mirror_too_large",
                            url=url[:120],
                            size_so_far=len(buf),
                        )
                        return None
    except httpx.HTTPError as exc:
        logger.warning("reference_mirror_fetch_failed", url=url[:120], error=str(exc))
        return None

    # Guess extension from content-type when the URL doesn't carry one
    ext_from_ct = mimetypes.guess_extension(content_type) or ".bin"
    filename = _filename_from_url(url, fallback_ext=ext_from_ct)
    key = s3.make_key(project_id, kind, filename)
    try:
        s3.upload_bytes(key, bytes(buf), content_type=content_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reference_mirror_upload_failed", key=key, error=str(exc))
        return None
    return key, content_type


def _commit_reference(session: Session, ref: ContentReference) -> ContentReference:
    session.add(ref)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="reference already imported (project_id + source_provider + source_external_id)",
        ) from exc
    session.refresh(ref)
    return ref


def manual_upload(
    session: Session, project_id: uuid.UUID, payload: ReferenceManualUpload, created_by: Optional[str] = None
) -> ContentReference:
    ref = ContentReference(
        project_id=project_id,
        source_provider="manual_upload",
        source_external_id=None,
        source_url=payload.source_url,
        media_s3_key=payload.media_s3_key,
        poster_s3_key=payload.poster_s3_key,
        caption=payload.caption,
        transcript=payload.transcript,
        hashtags=payload.hashtags,
        metadata_json=payload.metadata,
        status="approved" if payload.auto_approve else "candidate",
        imported_by=created_by or "manual",
    )
    return _commit_reference(session, ref)


def import_from_scraper(
    session: Session, project_id: uuid.UUID, payload: ReferenceImportFromScraper, created_by: Optional[str] = None
) -> ContentReference:
    """Pull an `ig_scraper.ig_posts` row across the cross-DB read engine."""
    raw = scraper_bridge.fetch_ig_post(payload.ig_post_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ig_post {payload.ig_post_id} not found in ig_scraper.ig_posts",
        )

    media_urls = raw.get("media_urls") or []
    metadata = {
        "shortcode": raw.get("shortcode"),
        "username": raw.get("username"),
        "taken_at": raw["taken_at"].isoformat() if raw.get("taken_at") else None,
        "like_count": raw.get("like_count"),
        "comment_count": raw.get("comment_count"),
        "play_count": raw.get("play_count"),
        "view_count": raw.get("view_count"),
        "media_type": raw.get("media_type"),
        "product_type": raw.get("product_type"),
        "score": float(raw["score"]) if raw.get("score") is not None else None,
        "ig_media_urls": media_urls,
        "ig_thumbnail_url": raw.get("thumbnail_url"),
    }
    source_url = (
        f"https://www.instagram.com/reel/{raw['shortcode']}/" if raw.get("shortcode") else None
    )

    # Mirror the first media URL to S3 synchronously so the admin panel
    # can show the asset right away. IG CDN URLs carry short-lived
    # signatures — if we don't fetch within minutes the URL 403s. Errors
    # are non-fatal: ref still gets created with media_s3_key=None and
    # the panel falls back to `metadata.ig_media_urls`.
    media_s3_key: Optional[str] = None
    poster_s3_key: Optional[str] = None
    # Phase 4 — also mirror each carousel slide so the img2img pipeline
    # has a stable init image for every scene_idx (IG CDN URLs expire
    # within days; we need permanence). Stored as a parallel S3 key
    # list in `metadata.slide_s3_keys[]`, index-aligned with
    # `ig_media_urls` so the panel and scenarios resolver can pick
    # `slide_s3_keys[i]` for scene i.
    slide_s3_keys: list[Optional[str]] = []
    media_type = raw.get("media_type")
    is_carousel = media_type == 8
    if media_urls:
        mirrored = _mirror_to_s3(media_urls[0], project_id, kind="references")
        if mirrored:
            media_s3_key = mirrored[0]
            slide_s3_keys.append(mirrored[0])
            if mirrored[1].startswith("image/"):
                poster_s3_key = mirrored[0]
        else:
            slide_s3_keys.append(None)
        # Carousel — keep mirroring the rest of the slides. Single-asset
        # sources (photo / reel) skip this loop. Failures stay non-fatal:
        # we just leave the slot as None and the resolver wraps around
        # the existing slides.
        if is_carousel and len(media_urls) > 1:
            for slide_url in media_urls[1:]:
                mirrored_slide = _mirror_to_s3(
                    slide_url, project_id, kind="references"
                )
                slide_s3_keys.append(mirrored_slide[0] if mirrored_slide else None)
    # Separate thumbnail when ig_scraper supplies one and we haven't
    # already promoted the media as a poster.
    if poster_s3_key is None and raw.get("thumbnail_url"):
        thumb = _mirror_to_s3(raw["thumbnail_url"], project_id, kind="references")
        if thumb:
            poster_s3_key = thumb[0]
    # Stamp slide keys into metadata so the resolver can find them
    # without re-mirroring. Only set when at least one slide mirrored;
    # otherwise leave it absent and let `compute_init_keys` fall back
    # to media_s3_key/poster_s3_key.
    if any(slide_s3_keys):
        metadata["slide_s3_keys"] = slide_s3_keys

    ref = ContentReference(
        project_id=project_id,
        source_provider="instagram",
        source_external_id=str(raw["source_external_id"]),
        source_url=source_url,
        media_s3_key=media_s3_key,
        poster_s3_key=poster_s3_key,
        caption=raw.get("caption"),
        transcript=None,
        hashtags=None,
        metadata_json=metadata,
        status="approved" if payload.auto_approve else "candidate",
        imported_by=created_by or "import-from-scraper",
    )
    committed = _commit_reference(session, ref)

    # Phase 4 — kick off reel keyframe extraction. Photo / carousel
    # sources already have a per-slide init image via
    # `metadata.slide_s3_keys`; only video sources need ffmpeg.
    if (media_type == 2 or (raw.get("product_type") or "").lower() in ("clips", "reels")) and media_s3_key:
        enqueue_frame_extract(committed)

    return committed


def _copy_mirrored_object(source_key: str, project_id: uuid.UUID) -> Optional[str]:
    """Server-side copy of another service's mirrored object into our prefix.

    Returns the new key, or None on failure (non-fatal — the caller falls
    back to referencing the source key in place). No bytes pass through this
    process; S3 does the copy.
    """
    if not source_key or not s3.is_configured():
        return None
    filename = os.path.basename(source_key) or "asset.bin"
    dest_key = s3.make_key(project_id, "references", filename)
    try:
        return s3.copy_object(source_key, dest_key)
    except Exception as exc:  # noqa: BLE001 — boto3 raises a wide family
        logger.warning(
            "reference_s3_copy_failed",
            source_key=source_key[:160],
            dest_key=dest_key[:160],
            error=str(exc),
        )
        return None


def import_from_ads(
    session: Session,
    project_id: uuid.UUID,
    payload: ReferenceImportFromAds,
    created_by: Optional[str] = None,
) -> ContentReference:
    """Pull an `ad_scraper.ad_materials` row into the reference pool.

    No download happens here. ad_scraper already mirrored the creative into
    the shared bucket at ingestion time — YouCloud's signed CDN URLs expire
    roughly 15 days out, so waiting until import would often mean fetching a
    403. We either copy that object into this project's prefix (default) or
    reference it in place.

    A creative with no mirror still imports: the row is created with
    `media_s3_key=None` and `metadata.source_media_url` for the panel to try,
    alongside `metadata.source_media_expires_at` so it can tell the operator
    whether that URL is already dead.
    """
    raw = ad_scraper_bridge.fetch_ad_material(payload.material_id)
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"material {payload.material_id} not found in ad_scraper.ad_materials",
        )

    def _iso(value):
        return value.isoformat() if value is not None and hasattr(value, "isoformat") else value

    metadata = {
        # Ad-intelligence signals. `impressions` is the field an
        # `auto_generation_rules.pick_strategy` can meaningfully sort on.
        "impressions": raw.get("impression_inc_2y"),
        "impressions_label": raw.get("impression_inc_2y_raw"),
        # DAYS on air — a long-running creative is a proven creative. Not
        # to be confused with `media_duration_sec` below.
        "run_days": raw.get("run_days"),
        "ad_count": raw.get("ad_count"),
        "similar_count": raw.get("similar_cnt"),
        "media_duration_sec": raw.get("media_duration_sec"),
        "media_format": raw.get("media_format"),
        "media_width": raw.get("media_width"),
        "media_height": raw.get("media_height"),
        "material_type": raw.get("material_type"),
        "first_on_air": _iso(raw.get("start_date")),
        "last_on_air": _iso(raw.get("end_date")),
        "gender_target": raw.get("gender"),
        "violation": raw.get("violation"),
        "media_channels": raw.get("media_names") or [],
        "areas": raw.get("area_codes") or [],
        "platforms": raw.get("platform_names") or [],
        "advertisers": raw.get("advertiser_names") or [],
        # Fallbacks for a creative whose mirror is missing. The expiry lets
        # the panel say "dead link" instead of showing a broken player.
        "source_media_url": raw.get("media_url"),
        "source_poster_url": raw.get("poster_url"),
        "source_media_expires_at": _iso(raw.get("media_url_expires_at")),
        "ad_scraper_media_s3_key": raw.get("media_s3_key"),
        "ad_scraper_poster_s3_key": raw.get("poster_s3_key"),
    }

    source_media_key = raw.get("media_s3_key")
    source_poster_key = raw.get("poster_s3_key")
    media_s3_key: Optional[str] = None
    poster_s3_key: Optional[str] = None

    if payload.copy_media:
        media_s3_key = _copy_mirrored_object(source_media_key, project_id) if source_media_key else None
        if source_poster_key and source_poster_key == source_media_key:
            # An image creative is its own poster — reuse the single copy.
            poster_s3_key = media_s3_key
        elif source_poster_key:
            poster_s3_key = _copy_mirrored_object(source_poster_key, project_id)
        # A failed copy is not a failed import; fall back to referencing
        # ad_scraper's object rather than losing the asset entirely.
        media_s3_key = media_s3_key or source_media_key
        poster_s3_key = poster_s3_key or source_poster_key
    else:
        media_s3_key = source_media_key
        poster_s3_key = source_poster_key

    ref = ContentReference(
        project_id=project_id,
        source_provider="appgrowing",
        source_external_id=str(raw["source_external_id"]),
        source_url=raw.get("txt_url") or None,
        media_s3_key=media_s3_key,
        poster_s3_key=poster_s3_key,
        # The ad's own copy line is the closest thing to a caption.
        caption=raw.get("slogan") or raw.get("description"),
        # ASR is the platform's auto-transcript. Populated on roughly a fifth
        # of video creatives, and the single most useful input the analyzer
        # gets from an ad.
        transcript=raw.get("asr") or None,
        hashtags=None,
        metadata_json=metadata,
        status="approved" if payload.auto_approve else "candidate",
        imported_by=created_by or "import-from-ads",
    )
    committed = _commit_reference(session, ref)

    # Video creatives get the ffmpeg keyframe pass so scenarios have per-scene
    # init images, same as an imported reel. `material_type` 202 = video.
    if raw.get("material_type") == 202 and committed.media_s3_key:
        enqueue_frame_extract(committed)

    return committed


def enqueue_frame_extract(reference: ContentReference) -> Optional[str]:
    """Queue the ffmpeg keyframe + scene-boundary pass for a reel.

    Soft-fails when Redis is down — the reference is still usable, the
    admin just re-triggers from the panel.
    """
    try:
        from app.services import queue as queue_svc

        job = queue_svc.enqueue(
            "frame_extract",
            "app.workers.reference_frame_extract.run",
            str(reference.id),
        )
        return job.id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ref_frame_extract_enqueue_failed",
            reference_id=str(reference.id),
            error=str(exc),
        )
        return None


def remirror_media(session: Session, reference: ContentReference) -> ContentReference:
    """Re-download the source media into S3.

    `_mirror_to_s3` is fail-open at import time, so `media_s3_key` can be
    NULL when the IG CDN signature expired between the scrape and the
    import. Repurpose mode has nothing to cut in that case, so this is
    the recovery path: retry the stored CDN URL, and if that is dead,
    ask the scraper for a freshly-signed one.
    """
    meta = dict(reference.metadata_json or {})
    urls = meta.get("ig_media_urls") or []
    candidate = urls[0] if isinstance(urls, list) and urls else None

    mirrored = _mirror_to_s3(candidate, reference.project_id) if candidate else None

    if mirrored is None and reference.source_external_id:
        # Stored URL is dead — pull a fresh signature from the scraper.
        try:
            raw = scraper_bridge.fetch_ig_post(reference.source_external_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"could not re-fetch the source post: {exc}",
            ) from exc
        fresh = (raw or {}).get("media_urls") or []
        if fresh:
            meta["ig_media_urls"] = fresh
            mirrored = _mirror_to_s3(fresh[0], reference.project_id)

    if mirrored is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="could not mirror the source media — the CDN URL is unreachable",
        )

    key, content_type = mirrored
    reference.media_s3_key = key
    meta["media_content_type"] = content_type
    if content_type.startswith("image/"):
        reference.poster_s3_key = key
    else:
        # A fresh video invalidates the old frame set — clearing the keys
        # makes the extractor re-run instead of short-circuiting.
        meta.pop("frame_s3_keys", None)
        meta.pop("frame_records", None)
        meta.pop("scene_boundaries_sec", None)
    reference.metadata_json = meta
    session.add(reference)
    session.flush()
    session.refresh(reference)

    if not content_type.startswith("image/"):
        enqueue_frame_extract(reference)
    return reference


def list_(
    session: Session,
    project_id: uuid.UUID,
    status_: Optional[str] = None,
    source_provider: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[ContentReference]:
    stmt = select(ContentReference).where(ContentReference.project_id == project_id)
    if status_:
        stmt = stmt.where(ContentReference.status == status_)
    if source_provider:
        stmt = stmt.where(ContentReference.source_provider == source_provider)
    stmt = stmt.order_by(ContentReference.imported_at.desc()).limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get(session: Session, project_id: uuid.UUID, reference_id: uuid.UUID) -> ContentReference:
    row = session.get(ContentReference, reference_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="reference not found")
    return row


def update(
    session: Session, project_id: uuid.UUID, reference_id: uuid.UUID, payload: ReferenceUpdate
) -> ContentReference:
    row = get(session, project_id, reference_id)
    data = payload.model_dump(exclude_unset=True)
    if "metadata" in data:
        row.metadata_json = data.pop("metadata")
    for key, value in data.items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def archive(session: Session, project_id: uuid.UUID, reference_id: uuid.UUID) -> ContentReference:
    row = get(session, project_id, reference_id)
    row.status = "archived"
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


def usage_check(session: Session, project: Project, reference_id: uuid.UUID) -> UsageCheck:
    """Return reuse history + the project's policy."""
    get(session, project.id, reference_id)  # ensure exists & belongs to project

    stmt = (
        select(ReferenceUsage)
        .where(ReferenceUsage.reference_id == reference_id)
        .order_by(ReferenceUsage.created_at.desc())
    )
    rows = list(session.exec(stmt).all())
    last_used_days_ago = None
    if rows:
        delta = datetime.now(timezone.utc) - rows[0].created_at
        last_used_days_ago = max(delta.days, 0)
    previous = [
        {
            "scenario_id": str(r.scenario_id),
            "status": r.status,
            "created_at": r.created_at.isoformat(),
            "reuse_reason": r.reuse_reason or None,
        }
        for r in rows
    ]
    return UsageCheck(
        reference_id=reference_id,
        previously_used=bool(rows),
        usage_count=len(rows),
        last_used_days_ago=last_used_days_ago,
        previous_scenarios=previous,
        project_reuse_policy=project.reuse_policy,
    )
