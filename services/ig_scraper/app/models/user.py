"""ig_users — Instagram users observed during scraping (the targets)."""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import utcnow


class IgUser(SQLModel, table=True):
    """An Instagram user we have at least seen once.

    PK is IG's numeric `pk`. Stats columns reflect the last snapshot we
    fetched; `raw` carries the full payload for forensic re-parsing.
    """

    __tablename__ = "ig_users"

    id: int = Field(primary_key=True)
    username: str = Field(unique=True, index=True)
    full_name: Optional[str] = Field(default=None)
    biography: Optional[str] = Field(default=None)
    follower_count: Optional[int] = Field(default=None)
    following_count: Optional[int] = Field(default=None)
    media_count: Optional[int] = Field(default=None)
    is_business: Optional[bool] = Field(default=None)
    is_verified: Optional[bool] = Field(default=None)
    is_private: Optional[bool] = Field(default=None)
    profile_pic_url: Optional[str] = Field(default=None)
    biography_expires_at: Optional[datetime] = Field(default=None)  # GDPR TTL placeholder
    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
