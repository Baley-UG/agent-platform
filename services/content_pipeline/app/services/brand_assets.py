"""Brand asset library — service-layer CRUD.

The router stays thin; this module handles all DB writes / lookups,
presigned URL stamping, and RQ enqueuing for auto-tag jobs.

Distinct from `media_assets` service because brand assets have their
own lifecycle (admin upload, manual tagging, retag) that we don't want
mixing with pipeline-produced asset versioning.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core import s3
from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.schemas.brand_assets import (
    BrandAssetCreate,
    BrandAssetRead,
    BrandAssetTags,
    BrandAssetUpdate,
)
from app.services import queue as queue_svc


# media_assets.type values we accept as "brand library" rows. The
# library only ingests admin-curated assets (not pipeline intermediates
# like 'scene_image' or 'voiceover'). When the create endpoint runs we
# stamp `media_assets.type = 'brand_library'` so the GET filter is fast.
_BRAND_LIBRARY_TYPE = "brand_library"


def _signed_preview_url(asset: MediaAsset) -> Optional[str]:
    """Short-lived presigned GET URL for the panel grid. Falls back to
    None when S3 isn't configured (dev without MinIO running)."""
    if not asset.s3_key:
        return None
    try:
        return s3.presigned_get_url(asset.s3_key, ttl=900)
    except Exception:  # noqa: BLE001
        return None


def _to_read(asset: MediaAsset) -> BrandAssetRead:
    """Hand-roll the read model so we can stamp the presign URL."""
    tags_obj: Optional[BrandAssetTags] = None
    if asset.brand_asset_tags:
        try:
            tags_obj = BrandAssetTags.model_validate(asset.brand_asset_tags)
        except Exception:  # noqa: BLE001
            # Malformed tag dict (e.g. from a buggy tagger run); strip
            # rather than 500 the list. Admin can re-tag.
            logger.warning(
                "brand_asset_tags_invalid", asset_id=str(asset.id)
            )
            tags_obj = None
    return BrandAssetRead(
        id=asset.id,
        project_id=asset.project_id,
        brand_kit_id=asset.brand_kit_id,
        type=asset.type,
        brand_asset_type=asset.brand_asset_type,
        brand_asset_tags=tags_obj,
        s3_key=asset.s3_key,
        preview_url=_signed_preview_url(asset),
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        width=asset.width,
        height=asset.height,
        duration_sec=float(asset.duration_sec) if asset.duration_sec is not None else None,
        source_asset_id=asset.source_asset_id,
        source_timestamp_sec=(
            float(asset.source_timestamp_sec)
            if asset.source_timestamp_sec is not None
            else None
        ),
        auto_tagged_at=asset.auto_tagged_at,
        created_at=asset.created_at,
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create(
    session: Session,
    *,
    project_id: uuid.UUID,
    payload: BrandAssetCreate,
) -> MediaAsset:
    """Register an already-uploaded S3 object as a brand asset.

    `media_assets.type` is forced to 'brand_library' so the GET query
    can index-seek. We never trust the caller's `type` here — pipeline
    intermediates don't enter the brand library.
    """
    asset = MediaAsset(
        project_id=project_id,
        type=_BRAND_LIBRARY_TYPE,
        s3_key=payload.s3_key,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        width=payload.width,
        height=payload.height,
        duration_sec=payload.duration_sec,
        version=1,
        status="ready",
        brand_kit_id=payload.brand_kit_id,
        brand_asset_type=payload.brand_asset_type,
        brand_asset_tags=(
            payload.brand_asset_tags.model_dump(exclude_none=True)
            if payload.brand_asset_tags
            else None
        ),
    )
    session.add(asset)
    session.flush()
    session.refresh(asset)

    if payload.auto_tag:
        # Image uploads → vision tagger directly. Video uploads → frame
        # extractor first (the extractor enqueues per-frame tagging once
        # frames land). Soft-enqueue: Redis being down leaves the asset
        # in the library untagged; admin can retrigger via /retag or
        # /extract-frames.
        mime = (payload.mime_type or "").lower()
        if mime.startswith("video/"):
            try:
                queue_svc.enqueue(
                    "frame_extract",
                    "app.workers.video_frame_extract.run",
                    str(asset.id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "frame_extract_enqueue_failed",
                    asset_id=str(asset.id),
                    error=str(exc),
                )
        else:
            try:
                queue_svc.enqueue(
                    "brand_asset_tag",
                    "app.workers.brand_asset_tagger.run",
                    str(asset.id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "brand_asset_tag_enqueue_failed",
                    asset_id=str(asset.id),
                    error=str(exc),
                )

    return asset


def list_(
    session: Session,
    *,
    project_id: uuid.UUID,
    brand_kit_id: Optional[uuid.UUID] = None,
    brand_asset_type: Optional[str] = None,
    has_face: Optional[bool] = None,
    mood: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[MediaAsset]:
    """Return the brand library for a project, optionally filtered.

    `has_face` and `mood` are JSONB checks; we only filter when the
    caller sets them so the index `ix_media_assets_brand_kit_type`
    stays useful for the common case.
    """
    stmt = (
        select(MediaAsset)
        .where(
            MediaAsset.project_id == project_id,
            MediaAsset.type == _BRAND_LIBRARY_TYPE,
            MediaAsset.replaced_by_id.is_(None),
        )
        .order_by(MediaAsset.created_at.desc())
    )
    if brand_kit_id is not None:
        stmt = stmt.where(MediaAsset.brand_kit_id == brand_kit_id)
    if brand_asset_type is not None:
        stmt = stmt.where(MediaAsset.brand_asset_type == brand_asset_type)
    # JSONB filters — Postgres-native. SQLModel's .op syntax is the cleanest.
    if has_face is not None:
        # `brand_asset_tags->>'has_face'` is text; cast to bool.
        stmt = stmt.where(
            MediaAsset.brand_asset_tags["has_face"].astext == ("true" if has_face else "false")
        )
    if mood is not None:
        stmt = stmt.where(MediaAsset.brand_asset_tags["mood"].astext == mood.lower())
    stmt = stmt.limit(limit).offset(offset)
    return list(session.exec(stmt).all())


def get(session: Session, *, project_id: uuid.UUID, asset_id: uuid.UUID) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None or asset.project_id != project_id or asset.type != _BRAND_LIBRARY_TYPE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="brand asset not found")
    return asset


def update(
    session: Session,
    asset: MediaAsset,
    payload: BrandAssetUpdate,
) -> MediaAsset:
    """Patch tags / type / kit. Tag dict is REPLACED, not merged —
    admin sees the full tag form in the panel and submits the whole
    thing back; partial-merge would surprise them."""
    data = payload.model_dump(exclude_unset=True)
    if "brand_kit_id" in data:
        asset.brand_kit_id = data["brand_kit_id"]
    if "brand_asset_type" in data:
        asset.brand_asset_type = data["brand_asset_type"]
    if "brand_asset_tags" in data:
        if data["brand_asset_tags"] is None:
            asset.brand_asset_tags = None
        else:
            # `data["brand_asset_tags"]` is a dict already (Pydantic
            # serialized the nested model). Strip None values so the
            # JSONB stays compact.
            asset.brand_asset_tags = {
                k: v for k, v in data["brand_asset_tags"].items() if v is not None
            }
    session.add(asset)
    session.flush()
    session.refresh(asset)
    return asset


def delete(session: Session, asset: MediaAsset) -> None:
    """Soft-delete the row. Hard delete would orphan S3 objects and
    break any `scene_renders` that already resolved to this asset.
    Mark deleted; the matcher filters by `status='ready'`."""
    asset.status = "deleted"
    asset.replaced_by_id = asset.id  # so it stops showing in active lists
    session.add(asset)
    session.flush()


def enqueue_retag(asset: MediaAsset) -> Optional[str]:
    """Push a retag job; returns the RQ job id, or None on enqueue failure."""
    try:
        job = queue_svc.enqueue(
            "brand_asset_tag",
            "app.workers.brand_asset_tagger.run",
            str(asset.id),
        )
        return job.id if job else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brand_asset_retag_enqueue_failed",
            asset_id=str(asset.id),
            error=str(exc),
        )
        return None


def enqueue_frame_extract(asset: MediaAsset) -> Optional[str]:
    """Push a frame-extract job for a video asset.

    Caller (`POST /brand-assets/{id}/extract-frames`) is expected to
    have deleted any previous extracted children first if a re-extract
    is the goal — the worker is idempotent and bails out when children
    already exist.
    """
    try:
        job = queue_svc.enqueue(
            "frame_extract",
            "app.workers.video_frame_extract.run",
            str(asset.id),
        )
        return job.id if job else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brand_asset_extract_enqueue_failed",
            asset_id=str(asset.id),
            error=str(exc),
        )
        return None
