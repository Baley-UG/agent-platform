"""ig_audio_tracks — normalised reels audio metadata."""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel

from app.models.base import utcnow


class AudioTrack(SQLModel, table=True):
    """A piece of audio used in one or more reels."""

    __tablename__ = "ig_audio_tracks"

    id: str = Field(primary_key=True, description="IG audio cluster id.")
    title: Optional[str] = Field(default=None)
    artist: Optional[str] = Field(default=None)
    original_audio_user_id: Optional[int] = Field(default=None, foreign_key="ig_users.id")
    duration_ms: Optional[int] = Field(default=None)
    use_count: int = Field(default=0)
    raw: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
