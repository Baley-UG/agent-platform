"""ig_scan_targets — daily-scan registry."""

import uuid
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint

from app.models.base import new_uuid, utcnow


class ScanTarget(SQLModel, table=True):
    """A persistent declaration that a username or hashtag should be scanned periodically.

    The scheduler reads this table every minute and turns due rows into
    queued jobs. Cursor columns (`first_backfill_done`, `last_seen_*`)
    are updated by the worker on completion.
    """

    __tablename__ = "ig_scan_targets"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_scan_targets_kind_value"),)

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    kind: str = Field(index=True)  # user | hashtag
    value: str = Field(index=True)
    status: str = Field(default="active", index=True)  # active | paused | pending_review
    interval_hours: int = Field(default=24)
    fetch_feed: bool = Field(default=True)
    fetch_stories: bool = Field(default=True)
    fetch_highlights: bool = Field(default=False)
    fetch_comments: bool = Field(default=True)
    comment_limit: int = Field(default=50)
    min_likes: Optional[int] = Field(default=None)
    min_impressions: Optional[int] = Field(default=None)
    hashtag_section: str = Field(default="top")  # top | recent
    first_backfill_done: bool = Field(default=False)
    last_seen_post_id: Optional[int] = Field(default=None)
    last_seen_taken_at: Optional[datetime] = Field(default=None)
    last_run_at: Optional[datetime] = Field(default=None)
    next_run_at: datetime = Field(default_factory=utcnow, index=True)
    last_run_job_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_scrape_jobs.id")
    auto_discovered: bool = Field(default=False)
    source_target_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_scan_targets.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
