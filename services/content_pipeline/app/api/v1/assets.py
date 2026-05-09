"""Asset upload — presigned URLs for direct admin/browser → S3 uploads.

We never proxy bytes through the API. The admin panel calls
`POST /assets/upload-url`, gets back a short-lived PUT URL + the final S3
key, then PUTs the bytes directly. After the upload completes the panel
patches the relevant resource (brand_kit / template / music_track) with
the returned S3 key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.api.v1.deps import get_project, require_api_key
from app.core import s3
from app.models.projects import Project
from app.schemas.assets import PresignedUploadRequest, PresignedUploadResponse
from app.core.config import settings
from app.core.metrics import cp_assets_uploaded_total

router = APIRouter(
    prefix="/projects/{project_id}/assets",
    tags=["assets"],
    dependencies=[Depends(require_api_key)],
)

# Map upload kinds to the S3 prefix they live under.
_KIND_TO_PREFIX = {
    "brand_logo": "brand_assets",
    "template_video": "templates",
    "music": "music",
    "reference_media": "references",
    "misc": "misc",
}


@router.post("/upload-url", response_model=PresignedUploadResponse, status_code=status.HTTP_200_OK)
def create_upload_url(
    payload: PresignedUploadRequest,
    project: Project = Depends(get_project),
) -> PresignedUploadResponse:
    """Generate a presigned PUT URL for the admin panel to upload directly to S3."""
    prefix_kind = _KIND_TO_PREFIX[payload.kind]
    key = s3.make_key(project.id, prefix_kind, payload.filename)
    url = s3.presigned_put_url(key, content_type=payload.content_type)
    cp_assets_uploaded_total.labels(kind=payload.kind).inc()
    headers: dict = {}
    if payload.content_type:
        headers["Content-Type"] = payload.content_type
    return PresignedUploadResponse(
        upload_url=url,
        s3_key=key,
        expires_in=settings.S3_PRESIGNED_URL_TTL_SECONDS,
        required_headers=headers,
    )
