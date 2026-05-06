"""ig_highlights and ig_highlight_items."""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import utcnow


class Highlight(SQLModel, table=True):
    """A user's highlight reel container."""

    __tablename__ = "ig_highlights"

    id: int = Field(primary_key=True)
    owner_id: int = Field(foreign_key="ig_users.id", index=True)
    title: Optional[str] = Field(default=None)
    cover_url: Optional[str] = Field(default=None)
    media_count: Optional[int] = Field(default=None)
    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    last_scanned_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)


class HighlightItem(SQLModel, table=True):
    """Membership of a Story inside a Highlight."""

    __tablename__ = "ig_highlight_items"

    highlight_id: int = Field(foreign_key="ig_highlights.id", primary_key=True)
    story_id: int = Field(foreign_key="ig_stories.id", primary_key=True)
    position: int = Field(default=0)
