"""media_assets endpoints — preview URLs (presigned GET) + version history.

Admin panel needs both:
- a way to display image/video bytes from a private S3 bucket
- a way to show the version chain (regenerate history) per asset
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.core import s3
from app.core.config import settings
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.schemas.media_assets import AssetHistoryResponse, MediaAssetRead, PresignedReadResponse
from app.services import media_assets as svc

router = APIRouter(
    prefix="/projects/{project_id}/media-assets",
    tags=["media-assets"],
    dependencies=[Depends(require_api_key)],
)


def _scoped(session: Session, project: Project, asset_id: uuid.UUID) -> MediaAsset:
    asset = session.get(MediaAsset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_asset not found")
    return asset


@router.get("", response_model=list[MediaAssetRead])
def list_assets(
    parent_scenario_id: Optional[uuid.UUID] = Query(default=None),
    type_: Optional[str] = Query(default=None, alias="type"),
    active_only: bool = Query(
        default=True,
        description="Only the active version of each chain (replaced_by_id IS NULL).",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> list[MediaAssetRead]:
    """Filtered asset listing.

    The panel uses this for per-scene voiceover clips
    (`?parent_scenario_id=…&type=voiceover_scene`) — the scenario's
    `voiceover_asset_id` only points at clip 0 in per-scene mode, so
    playing the full narration requires listing the clips. Generic
    filters so other panel views (all finals of a scenario, etc.) can
    reuse it without new endpoints.
    """
    from sqlmodel import select

    stmt = select(MediaAsset).where(MediaAsset.project_id == project.id)
    if parent_scenario_id is not None:
        stmt = stmt.where(MediaAsset.parent_scenario_id == parent_scenario_id)
    if type_ is not None:
        stmt = stmt.where(MediaAsset.type == type_)
    if active_only:
        stmt = stmt.where(MediaAsset.replaced_by_id.is_(None))
    stmt = (
        stmt.order_by(MediaAsset.parent_scene_idx, MediaAsset.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [MediaAssetRead.model_validate(a) for a in session.exec(stmt).all()]


@router.get("/{asset_id}", response_model=MediaAssetRead)
def get_asset(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> MediaAssetRead:
    return MediaAssetRead.model_validate(_scoped(session, project, asset_id))


@router.get("/{asset_id}/preview-url", response_model=PresignedReadResponse)
def preview_url(
    asset_id: uuid.UUID,
    ttl: Optional[int] = Query(default=None, ge=60, le=86400, description="Override expiry seconds (default from config)."),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> PresignedReadResponse:
    """Return a short-lived presigned GET URL the browser can fetch directly from S3."""
    asset = _scoped(session, project, asset_id)
    url = s3.presigned_get_url(asset.s3_key, ttl=ttl)
    return PresignedReadResponse(
        asset_id=asset.id,
        s3_key=asset.s3_key,
        preview_url=url,
        expires_in=ttl or settings.S3_PRESIGNED_URL_TTL_SECONDS,
    )


@router.get("/{asset_id}/history", response_model=AssetHistoryResponse)
def history(
    asset_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> AssetHistoryResponse:
    """Return the full version chain for this asset — oldest first.

    The caller can pass any version id (active, intermediate, or root) and
    we walk the `previous_version_id` / `replaced_by_id` links to assemble
    the complete history.
    """
    seed = _scoped(session, project, asset_id)
    chain = svc.walk_chain(session, seed.id)
    if not chain:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="empty version chain")
    return AssetHistoryResponse(
        asset_id=seed.id,
        versions=[MediaAssetRead.model_validate(v) for v in chain],
        current_version=chain[-1].version,
    )
