"""`content_references` — source-agnostic reference pool (IG / TikTok / manual upload)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class ContentReference(SQLModel, table=True):
    """A reference piece of content imported from a scraper or uploaded manually.

    `source_provider='manual_upload'` is the catch-all for content the user
    drops in directly (no scraper involved). The unique key tolerates a NULL
    `source_external_id` for that case via the partial index defined in the
    initial migration.
    """

    __tablename__ = "content_references"
    __table_args__ = (
        sa.Index("ix_content_references_project_status_imported", "project_id", "status", "imported_at"),
        sa.Index("ix_content_references_project_provider_external", "project_id", "source_provider", "source_external_id"),
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

    # 'instagram' | 'tiktok' | 'manual_upload'
    source_provider: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))
    source_external_id: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))
    source_url: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    media_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    poster_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))

    caption: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))
    transcript: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))
    hashtags: Optional[List[str]] = Field(default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True))

    metadata_json: Optional[dict] = Field(default=None, sa_column=sa.Column("metadata", JSONB, nullable=True))

    # 'candidate' | 'approved' | 'archived'
    status: str = Field(default="candidate", sa_column=sa.Column(sa.String(32), nullable=False))

    imported_by: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))
    imported_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
