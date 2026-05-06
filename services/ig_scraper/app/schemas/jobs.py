"""Pydantic schemas for /api/v1/jobs."""

import uuid
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

# Job types the queue understands. The worker dispatcher routes by this
# value, so it doubles as the API enum and the internal switch.
JobType = Literal[
    "user_feed_full",
    "user_feed_incremental",
    "user_stories",
    "user_highlights",
    "hashtag_top",
    "hashtag_recent",
    "user_enrich",
    # Phase 2 placeholders so the API contract is forward-compatible.
    "embed_post_batch",
    "extract_llm_features_batch",
]

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobCreate(BaseModel):
    """Body for POST /jobs.

    `target` is the username (no leading `@`) or hashtag (no leading
    `#`). `params` is a free-form bag for job-type-specific knobs;
    its shape is documented in the plan per scraper.
    """

    job_type: JobType
    target: str = Field(min_length=1, max_length=128)
    priority: int = Field(default=100, ge=0, le=10_000)
    params: Optional[Dict[str, Any]] = None
    min_likes: Optional[int] = Field(default=None, ge=0)
    min_impressions: Optional[int] = Field(default=None, ge=0)
    scheduled_for: Optional[datetime] = Field(
        default=None,
        description="Earliest moment the worker may pick this up. Defaults to now().",
    )
    max_attempts: int = Field(default=3, ge=1, le=10)


class JobRead(BaseModel):
    """Read shape — full job row including stats and error."""

    id: uuid.UUID
    job_type: str
    target: str
    scan_target_id: Optional[uuid.UUID]
    status: JobStatus
    priority: int
    params: Optional[Dict[str, Any]]
    min_likes: Optional[int]
    min_impressions: Optional[int]
    account_id: Optional[uuid.UUID]
    proxy_id: Optional[uuid.UUID]
    attempt: int
    max_attempts: int
    error: Optional[str]
    stats: Optional[Dict[str, Any]]
    scheduled_for: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime
