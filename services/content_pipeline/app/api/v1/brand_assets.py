"""Brand asset library — REST endpoints.

`POST /projects/{pid}/brand-assets` registers an already-uploaded S3
object. The upload itself uses the existing presigned-PUT flow
(`POST /projects/{pid}/assets/upload-url`); admin gets a URL, PUTs the
bytes, then calls this endpoint with the returned `s3_key`.

This is the same shape as `POST /references/upload`, deliberately —
admins already know the flow.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.models.projects import Project
from app.schemas.brand_assets import (
    BrandAssetCreate,
    BrandAssetRead,
    BrandAssetUpdate,
    RetagResult,
)
from app.services import brand_assets as svc

router = APIRouter(
    prefix="/projects/{project_id}/brand-assets",
    tags=["brand-assets"],
    dependencies=[Depends(require_api_key)],
)


@router.post("", response_model=BrandAssetRead, status_code=status.HTTP_201_CREATED)
def create_brand_asset(
    payload: BrandAssetCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> BrandAssetRead:
    """Register an S3-uploaded file as a brand-library asset. Auto-tag
    fires async via RQ when `auto_tag=true` (default)."""
    asset = svc.create(session, project_id=project.id, payload=payload)
    return svc._to_read(asset)


@router.get("", response_model=List[BrandAssetRead])
def list_brand_assets(
    brand_kit_id: Optional[uuid.UUID] = Query(default=None),
    brand_asset_type: Optional[str] = Query(default=None),
    has_face: Optional[bool] = Query(default=None),
    mood: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[BrandAssetRead]:
    """Browse the project's brand library. Filters compose via AND."""
    rows = svc.list_(
        session,
        project_id=project.id,
        brand_kit_id=brand_kit_id,
        brand_asset_type=brand_asset_type,
        has_face=has_face,
        mood=mood,
        limit=limit,
        offset=offset,
    )
    return [svc._to_read(r) for r in rows]


@router.get("/{asset_id}", response_model=BrandAssetRead)
def get_brand_asset(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> BrandAssetRead:
    asset = svc.get(session, project_id=project.id, asset_id=asset_id)
    return svc._to_read(asset)


@router.patch("/{asset_id}", response_model=BrandAssetRead)
def update_brand_asset(
    asset_id: uuid.UUID,
    payload: BrandAssetUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> BrandAssetRead:
    asset = svc.get(session, project_id=project.id, asset_id=asset_id)
    asset = svc.update(session, asset, payload)
    return svc._to_read(asset)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_brand_asset(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> None:
    asset = svc.get(session, project_id=project.id, asset_id=asset_id)
    svc.delete(session, asset)


@router.post(
    "/{asset_id}/retag",
    response_model=RetagResult,
    summary="Re-run the vision auto-tagger on an asset",
)
def retag_brand_asset(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RetagResult:
    """Async retag — enqueues the worker, returns the current state
    immediately. Panel polls the asset row to see the updated tags.
    Useful when:
      - the original tagger run failed (timeout, parse error)
      - admin updated the asset content via re-upload
      - the LLM route changed and admin wants better tags
    """
    asset = svc.get(session, project_id=project.id, asset_id=asset_id)
    svc.enqueue_retag(asset)
    return RetagResult(
        asset_id=asset.id,
        brand_asset_type=asset.brand_asset_type,
        brand_asset_tags=svc._to_read(asset).brand_asset_tags,
        cost_usd=None,
        latency_ms=None,
    )


@router.post(
    "/{asset_id}/extract-frames",
    status_code=status.HTTP_202_ACCEPTED,
    summary="(Re-)extract keyframes from a brand video asset",
)
def extract_video_frames(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    """Phase 3 — enqueue the ffmpeg keyframe extractor.

    No-op for non-video assets (the worker bails). On first run after
    upload, the create-asset flow already enqueues this; the manual
    endpoint exists for:
      - re-extraction after admin replaced the video content
      - retry when the first run failed (worker logged but the asset
        is otherwise intact)

    Returns 202 — the panel polls `GET /brand-assets?...` to see the
    extracted children appear with `source_asset_id == this id`.
    """
    asset = svc.get(session, project_id=project.id, asset_id=asset_id)
    svc.enqueue_frame_extract(asset)
    return {"asset_id": str(asset.id), "queued": True}
