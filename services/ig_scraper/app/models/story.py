"""ig_stories — ephemeral 24h stories."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Column, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class Story(SQLModel, table=True):
    """A live IG story.

    Insert-only — once a story expires we never re-fetch it. Missing a
    daily run = permanently lost stories for that day.
    """

    __tablename__ = "ig_stories"

    id: int = Field(primary_key=True)
    author_id: int = Field(foreign_key="ig_users.id", index=True)
    media_type: int = Field(description="1 photo, 2 video.")
    taken_at: datetime = Field(index=True)
    expires_at: datetime
    video_duration: Optional[float] = Field(default=None)
    media_url: Optional[str] = Field(default=None)
    thumbnail_url: Optional[str] = Field(default=None)
    caption: Optional[str] = Field(default=None)
    mentions: Optional[list[str]] = Field(default=None, sa_column=Column(ARRAY(String), nullable=True))
    hashtags: Optional[list[str]] = Field(default=None, sa_column=Column(ARRAY(String), nullable=True))
    link_sticker_url: Optional[str] = Field(default=None)
    seen_count: Optional[int] = Field(default=None)
    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    discovered_via_job_id: Optional[uuid.UUID] = Field(default=None, foreign_key="ig_scrape_jobs.id")
    captured_at: datetime = Field(default_factory=utcnow)
