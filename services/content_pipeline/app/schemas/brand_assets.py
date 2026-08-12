"""Brand asset library schemas.

A "brand asset" is a `media_assets` row with `brand_asset_type` set.
It lives in the brand's reusable pool and is matched by the director
against scene requirements before any AI synthesis fallback runs.

The taxonomy is intentionally short — admin doesn't have to think about
fine-grained categories. Vision auto-tag fills the rest (`brand_asset_tags`
JSONB) so semantic match (CLIP/embedding) does the heavy lifting later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# Taxonomy. Strings (not an enum) so the backend can extend without a
# breaking change; the panel hard-codes the dropdown to the canonical
# set below. "auto" is a sentinel used in `BrandAssetCreate` when the
# admin wants the vision tagger to pick the type itself.
BrandAssetType = Literal[
    "hero_product",
    "lifestyle",
    "face_talking",
    "face_action",
    "logo_card",
    "text_card",
    "b_roll",
    "product_detail",
    "misc",
]


class BrandAssetTags(BaseModel):
    """Vision-auto-tagged metadata. All fields optional — admins can
    leave blanks for the tagger to fill in, or override after it runs."""

    mood: Optional[str] = Field(
        default=None,
        description="Single dominant mood, e.g. 'luxe', 'energetic', 'calm', 'playful'.",
    )
    dominant_colors: Optional[List[str]] = Field(
        default=None,
        description="Top 3-5 hex colors sampled from the asset.",
    )
    subjects: Optional[List[str]] = Field(
        default=None,
        description="Identifiable objects/subjects: bottle, hand, face, logo, text, etc.",
    )
    has_face: Optional[bool] = Field(
        default=None,
        description="True if the frame contains a clearly identifiable human face.",
    )
    motion_intensity: Optional[Literal["still", "slow", "energetic"]] = Field(
        default=None,
        description="Video assets only. Still = static / sub-second motion.",
    )
    tags: Optional[List[str]] = Field(
        default=None, description="Free-form admin labels for filtering."
    )


class BrandAssetCreate(BaseModel):
    """Register an already-uploaded S3 object as a brand asset.

    Mirrors the `manual upload` reference flow: admin calls
    `/assets/upload-url` first, PUTs the bytes, then posts this body
    with the returned `s3_key`. Vision auto-tag fires on success and
    fills the missing tag fields asynchronously.
    """

    s3_key: str = Field(min_length=1, max_length=512)
    mime_type: Optional[str] = Field(default=None, max_length=128)
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[float] = None

    brand_kit_id: Optional[uuid.UUID] = None
    # Admin can pre-pin a type, or leave it None to let the vision
    # tagger guess. Manual override always wins later via PATCH.
    brand_asset_type: Optional[BrandAssetType] = None
    brand_asset_tags: Optional[BrandAssetTags] = None
    # Whether to enqueue the vision auto-tag job. Defaults to true; set
    # false for bulk-imports where the admin will tag manually.
    auto_tag: bool = True


class BrandAssetUpdate(BaseModel):
    """Partial edit. All fields nullable; null clears the field.

    Tag-only edits are the common case (re-mood, re-tag). Changing
    `brand_kit_id` moves the asset between kits."""

    brand_kit_id: Optional[uuid.UUID] = None
    brand_asset_type: Optional[BrandAssetType] = None
    brand_asset_tags: Optional[BrandAssetTags] = None


class BrandAssetRead(BaseModel):
    """Wire shape returned by GET/POST/PATCH endpoints.

    Includes a `preview_url` (presigned short-lived GET) so the panel
    grid can render thumbs without a separate round-trip per cell.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    brand_kit_id: Optional[uuid.UUID]
    type: str  # underlying `media_assets.type` (e.g. "reference_media", "brand_logo")
    brand_asset_type: Optional[str]
    brand_asset_tags: Optional[BrandAssetTags]

    s3_key: str
    preview_url: Optional[str] = None  # filled by the service layer

    mime_type: Optional[str]
    size_bytes: Optional[int]
    width: Optional[int]
    height: Optional[int]
    duration_sec: Optional[float]

    # Phase 3 — frame provenance. NULL on top-level assets (admin
    # uploads); set on rows that came from the ffmpeg keyframe pass.
    source_asset_id: Optional[uuid.UUID] = None
    source_timestamp_sec: Optional[float] = None

    auto_tagged_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class BrandAssetListParams(BaseModel):
    """Optional filters for `GET /brand-assets`. The router declares the
    fields as Query params individually; this class exists for typed
    tests and future client SDKs."""

    brand_kit_id: Optional[uuid.UUID] = None
    brand_asset_type: Optional[BrandAssetType] = None
    has_face: Optional[bool] = None
    mood: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class RetagResult(BaseModel):
    """`POST /brand-assets/{id}/retag` response — admin sees what the
    tagger inferred, can patch overrides immediately."""

    asset_id: uuid.UUID
    brand_asset_type: Optional[str]
    brand_asset_tags: Optional[BrandAssetTags]
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
