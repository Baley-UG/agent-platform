"""Read endpoints for ingested creatives.

`GET /materials/{id}/media-url` is the one that matters for a UI: the S3
bucket is private, so a browser cannot `<video src>` an object key. This
returns a short-lived presigned GET, the same pattern content_pipeline
uses for `media-assets/{id}/preview-url`.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.api.v1.deps import get_read_session, require_api_key
from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.services import queries

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("")
def list_materials(
    type: Optional[int] = Query(default=None, description="102 = image/banner, 202 = video."),
    media: Optional[List[str]] = Query(default=None, description="Media facet codes; repeatable (OR within)."),
    area: Optional[List[str]] = Query(default=None, description="Country codes, e.g. TR."),
    platform: Optional[List[str]] = Query(default=None),
    channel: Optional[List[str]] = Query(default=None),
    format: Optional[List[str]] = Query(default=None),
    advertiser_id: Optional[str] = Query(default=None),
    min_impressions: Optional[int] = Query(default=None, ge=0),
    min_run_days: Optional[int] = Query(default=None, ge=0),
    has_asr: Optional[bool] = Query(default=None, description="Only creatives with (or without) a transcript."),
    mirrored_only: bool = Query(default=False, description="Only creatives whose media is in our S3."),
    active_since: Optional[str] = Query(default=None, description="Still live on/after this date (YYYY-MM-DD)."),
    sort: str = Query(default=queries.DEFAULT_SORT),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_read_session),
) -> List[Dict[str, Any]]:
    """List creatives.

    Facets combine as AND across kinds and OR within a kind: `media=2&
    area=TR&area=DE` means "on media 2, in Turkey or Germany".
    """
    if sort not in queries.SORT_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"sort must be one of {sorted(queries.SORT_OPTIONS)}",
        )
    return queries.search_materials(
        session,
        media=media,
        area=area,
        platform=platform,
        channel=channel,
        format_=format,
        material_type=type,
        advertiser_id=advertiser_id,
        min_impressions=min_impressions,
        min_run_days=min_run_days,
        has_asr=has_asr,
        mirrored_only=mirrored_only,
        active_since=active_since,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/{material_id}")
def get_material(material_id: str, session: Session = Depends(get_read_session)) -> Dict[str, Any]:
    """One creative with its resources, facets and advertisers."""
    material = queries.get_material(session, material_id)
    if material is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="material not found")
    return material


@router.get("/{material_id}/media-url")
def get_media_url(
    material_id: str,
    kind: str = Query(default="media", pattern="^(media|poster)$"),
    ttl: int = Query(default=0, ge=0, le=86400, description="Seconds; 0 uses S3_PRESIGNED_URL_TTL_SECONDS."),
    session: Session = Depends(get_read_session),
) -> Dict[str, Any]:
    """Presigned GET for the mirrored asset.

    404 when the creative was never mirrored. In that case the caller can
    still try `media_url` from the material row, but note
    `media_url_expires_at` — past it, the CDN returns 403 and the bytes are
    gone for good.
    """
    material = queries.get_material(session, material_id)
    if material is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="material not found")

    key = material.get("media_s3_key") if kind == "media" else material.get("poster_s3_key")
    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"no mirrored {kind} for this creative. "
                "Re-run ingestion with mirroring enabled while the source URL is still valid "
                f"(expires_at={material.get('media_url_expires_at')})."
            ),
        )
    if not s3lib.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="S3 is not configured (S3_BUCKET / S3_ACCESS_KEY / S3_SECRET_KEY)",
        )

    try:
        url = s3lib.presigned_get_url(key, ttl or None)
    except Exception as exc:  # noqa: BLE001 — boto3 raises a wide family
        logger.warning("ad_presign_failed", material_id=material_id, key=key, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not presign {key}: {exc}",
        ) from exc

    return {
        "material_id": material_id,
        "kind": kind,
        "s3_key": key,
        "url": url,
        "expires_in": ttl or settings.S3_PRESIGNED_URL_TTL_SECONDS,
    }
