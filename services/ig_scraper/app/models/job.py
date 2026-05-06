"""ig_scrape_jobs — single source of truth for queued/running/finished work."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import new_uuid, utcnow


class ScrapeJob(SQLModel, table=True):
    """A scrape job claimed by the worker via SELECT ... FOR UPDATE SKIP LOCKED.

    `job_type` is one of:
      user_feed_full, user_feed_incremental, user_stories,
      user_highlights, hashtag_top, hashtag_recent, user_enrich,
      embed_post_batch (Phase 2), extract_llm_features_batch (Phase 2).
    """

    __tablename__ = "ig_scrape_jobs"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    job_type: str
    target: str
    scan_target_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_scan_targets.id")
    status: str = Field(default="queued", index=True)  # queued | running | succeeded | failed | cancelled
    priority: int = Field(default=100)
    params: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    min_likes: Optional[int] = Field(default=None)
    min_impressions: Optional[int] = Field(default=None)
    account_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_accounts.id")
    proxy_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_proxies.id")
    attempt: int = Field(default=0)
    max_attempts: int = Field(default=3)
    error: Optional[str] = Field(default=None)
    stats: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    scheduled_for: datetime = Field(default_factory=utcnow, index=True)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
