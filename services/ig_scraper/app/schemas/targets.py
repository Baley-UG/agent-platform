"""Pydantic schemas for /api/v1/targets."""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

TargetKind = Literal["user", "hashtag"]
TargetStatus = Literal["active", "paused", "pending_review"]
HashtagSection = Literal["top", "recent"]


class TargetCreate(BaseModel):
    """Body for POST /targets."""

    kind: TargetKind
    value: str = Field(min_length=1, max_length=128, description="Username (no @) or hashtag (no #).")
    interval_hours: int = Field(default=24, ge=1, le=24 * 7)
    fetch_feed: bool = True
    fetch_stories: bool = True
    fetch_highlights: bool = False
    fetch_comments: bool = True
    comment_limit: int = Field(default=50, ge=0, le=500)
    min_likes: Optional[int] = Field(default=None, ge=0)
    min_impressions: Optional[int] = Field(default=None, ge=0)
    hashtag_section: HashtagSection = "top"
    status: TargetStatus = "active"


class TargetUpdate(BaseModel):
    """Body for PATCH /targets/{id} — all fields optional."""

    interval_hours: Optional[int] = Field(default=None, ge=1, le=24 * 7)
    fetch_feed: Optional[bool] = None
    fetch_stories: Optional[bool] = None
    fetch_highlights: Optional[bool] = None
    fetch_comments: Optional[bool] = None
    comment_limit: Optional[int] = Field(default=None, ge=0, le=500)
    min_likes: Optional[int] = Field(default=None, ge=0)
    min_impressions: Optional[int] = Field(default=None, ge=0)
    hashtag_section: Optional[HashtagSection] = None
    status: Optional[TargetStatus] = None


class TargetRead(BaseModel):
    """Read shape — full row including cursors and provenance."""

    id: uuid.UUID
    kind: str
    value: str
    status: str
    interval_hours: int
    fetch_feed: bool
    fetch_stories: bool
    fetch_highlights: bool
    fetch_comments: bool
    comment_limit: int
    min_likes: Optional[int]
    min_impressions: Optional[int]
    hashtag_section: str
    first_backfill_done: bool
    last_seen_post_id: Optional[int]
    last_seen_taken_at: Optional[datetime]
    last_run_at: Optional[datetime]
    next_run_at: datetime
    last_run_job_id: Optional[uuid.UUID]
    auto_discovered: bool
    source_target_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime
