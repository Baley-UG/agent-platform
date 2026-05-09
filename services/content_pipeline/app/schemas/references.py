"""content_references schemas — manual upload, scraper import, list, approve."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

SourceProvider = Literal["instagram", "tiktok", "manual_upload"]
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
    """Pull an `ig_scraper.ig_posts` row into our reference pool."""

    ig_post_id: str = Field(min_length=1, description="Instagram media pk from ig_scraper.")
    auto_approve: bool = False


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
    caption: Optional[str]
    transcript: Optional[str]
    hashtags: Optional[List[str]]
    metadata_json: Optional[dict] = Field(default=None, alias="metadata")
    status: str
    imported_by: Optional[str]
    imported_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class UsageCheck(BaseModel):
    """Result of GET /references/{id}/usage-check."""

    reference_id: uuid.UUID
    previously_used: bool
    usage_count: int
    last_used_days_ago: Optional[int]
    previous_scenarios: List[dict]
    project_reuse_policy: str
