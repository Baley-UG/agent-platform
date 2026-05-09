"""`music_tracks` — uploaded, licensed music library."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class MusicTrack(SQLModel, table=True):
    """A music track owned by a project. Scraped audio is never stored here."""

    __tablename__ = "music_tracks"
    __table_args__ = (
        sa.Index("ix_music_tracks_project_id", "project_id"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    project_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.projects.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))

    audio_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    duration_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))
    bpm: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    mood: Optional[List[str]] = Field(default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True))
    tags: Optional[List[str]] = Field(default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True))

    # 'owned' | 'licensed' | 'public_domain'
    license: str = Field(default="owned", sa_column=sa.Column(sa.String(32), nullable=False))
    license_doc_url: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
