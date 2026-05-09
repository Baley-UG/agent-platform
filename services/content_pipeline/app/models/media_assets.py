"""`media_assets` — every artifact the pipeline produces or imports.

Versioned via (`version`, `replaced_by_id`). The currently-active asset is
the latest version with `replaced_by_id IS NULL`. Rollback swaps the chain;
nothing is hard-deleted by the regenerate flow.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class MediaAsset(SQLModel, table=True):
    """An S3-backed artifact. One row per version."""

    __tablename__ = "media_assets"
    __table_args__ = (
        sa.Index("ix_media_assets_project_type", "project_id", "type"),
        sa.Index("ix_media_assets_scenario", "parent_scenario_id"),
        sa.Index("ix_media_assets_replaced_by", "replaced_by_id"),
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

    # 'reference_media' | 'scene_image' | 'scene_video' | 'voiceover' | 'music'
    # | 'final_video' | 'thumbnail' | 'template_video' | 'brand_logo' | 'misc'
    type: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))

    s3_key: str = Field(sa_column=sa.Column(sa.String(512), nullable=False))
    mime_type: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(128), nullable=True))
    size_bytes: Optional[int] = Field(default=None, sa_column=sa.Column(sa.BigInteger, nullable=True))

    width: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    height: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    duration_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))

    parent_scenario_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    parent_scene_idx: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))

    # Versioning chain
    version: int = Field(default=1, sa_column=sa.Column(sa.Integer, nullable=False, server_default="1"))
    previous_version_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    replaced_by_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    metadata_json: Optional[dict] = Field(default=None, sa_column=sa.Column("metadata", JSONB, nullable=True))

    # 'ready' | 'deleted' | 'expired'
    status: str = Field(default="ready", sa_column=sa.Column(sa.String(32), nullable=False))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
