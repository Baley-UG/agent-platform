"""content_references schemas — manual upload, scraper import, list, approve."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator

SourceProvider = Literal["instagram", "tiktok", "manual_upload", "appgrowing"]
ReferenceStatus = Literal["candidate", "approved", "archived"]


class ReferenceManualUpload(BaseModel):
    """Create a reference from a file the admin already PUT to S3."""

    media_s3_key: str = Field(min_length=1, max_length=512)
    poster_s3_key: Optional[str] = Field(default=None, max_length=512)
    source_url: Optional[str] = None
    caption: Optional[str] = None
    transcript: Optional[str] = None
    hashtags: Optional[List[str]] = None
    metadata: Optional[dict] = None
    auto_approve: bool = True


class ReferenceImportFromScraper(BaseModel):
    """Pull an `ig_scraper.ig_posts` row into our reference pool.

    `ig_post_id` MUST be a JSON string. Instagram media pks are 19-digit
    integers (e.g. 3892567252147472686) which exceed Javascript's safe
    `Number.MAX_SAFE_INTEGER` (2^53−1 = 9007199254740991). Sending the
    pk as a JSON number from the admin panel silently rounds the last
    few digits — we accept ints here only as a defensive coercion, but
    the value's already been corrupted in transit.
    """

    ig_post_id: str = Field(
        min_length=1,
        description=(
            "Instagram media pk from ig_scraper, as a STRING. "
            "Sending as a JSON number loses precision past 2^53."
        ),
    )
    auto_approve: bool = False

    @field_validator("ig_post_id", mode="before")
    @classmethod
    def _coerce_int_to_str(cls, v):
        """Tolerate JSON numbers but warn that precision may already be lost."""
        if isinstance(v, int):
            return str(v)
        return v


class ReferenceImportFromAds(BaseModel):
    """Pull an `ad_scraper.ad_materials` row into our reference pool.

    Unlike the Instagram path this needs no download: `ad_scraper` mirrors
    every creative into the shared bucket at ingestion time (YouCloud's
    signed URLs expire ~15 days out), so the import is a server-side S3
    copy plus a row.

    `material_id` is AppGrowing's 32-hex creative id — a string, with none
    of the 2^53 precision trap Instagram pks have.
    """

    material_id: str = Field(
        min_length=1,
        max_length=64,
        description="AppGrowing material id (32-hex), as shown by ad_scraper's /materials.",
    )
    auto_approve: bool = False
    copy_media: bool = Field(
        default=True,
        description=(
            "Copy ad_scraper's mirrored object into this project's prefix. "
            "False references it in place — cheaper, but the row breaks if "
            "ad_scraper ever prunes its own prefix."
        ),
    )


class ReferenceUpdate(BaseModel):
    status: Optional[ReferenceStatus] = None
    caption: Optional[str] = None
    transcript: Optional[str] = None
    hashtags: Optional[List[str]] = None
    metadata: Optional[dict] = None


class ReferenceRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_provider: str
    source_external_id: Optional[str]
    source_url: Optional[str]
    media_s3_key: Optional[str]
    poster_s3_key: Optional[str]
    # Ready-to-use presigned GET URLs for the admin panel's `<img>` /
    # `<video>` tags. The service layer fills these in by signing the
    # corresponding *_s3_key when S3 is configured; the panel never
    # needs to round-trip to `/preview-url` for thumbnails. Both fall
    # back to the IG CDN URL stored in `metadata` when the S3 mirror
    # is missing.
    media_url: Optional[str] = None
    poster_url: Optional[str] = None
    caption: Optional[str]
    transcript: Optional[str]
    hashtags: Optional[List[str]]
    # When reading from the ORM object, look for `metadata_json` (the
    # real column). Do NOT alias to "metadata" — it collides with
    # SQLAlchemy's class-level `Base.metadata` MetaData object and
    # Pydantic ends up populating from that instead of the column.
    # Serialize OUT as `metadata` so the public JSON shape stays clean.
    metadata_json: Optional[dict] = Field(
        default=None,
        validation_alias=AliasChoices("metadata_json"),
        serialization_alias="metadata",
    )
    status: str
    imported_by: Optional[str]
    imported_at: datetime
    # How many remakes already spawned from this row. Drives the
    # "already remade" badge on the references grid so admins can spot
    # un-used candidates at a glance. Filled in by `svc.to_read`.
    remakes_count: int = 0

    model_config = {"from_attributes": True, "populate_by_name": True}


class UsageCheck(BaseModel):
    """Result of GET /references/{id}/usage-check."""

    reference_id: uuid.UUID
    previously_used: bool
    usage_count: int
    last_used_days_ago: Optional[int]
    previous_remakes: List[dict]
    project_reuse_policy: str
