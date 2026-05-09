"""Asset upload (presigned URL) schemas."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

UploadKind = Literal[
    "brand_logo",
    "template_video",
    "music",
    "reference_media",
    "misc",
]


class PresignedUploadRequest(BaseModel):
    kind: UploadKind
    filename: str = Field(min_length=1, max_length=255)
    content_type: Optional[str] = None


class PresignedUploadResponse(BaseModel):
    upload_url: str
    s3_key: str
    expires_in: int
    method: str = "PUT"
    required_headers: dict = Field(default_factory=dict)
