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
    # NOTE: the `ix_media_assets_brand_kit_type` partial index is declared
    # in the alembic migration only — SQLModel processes __table_args__
    # before the column Field declarations below, so referencing the new
    # brand_kit_id / brand_asset_type columns by name here raises
    # `ConstraintColumnNotFoundError` at import time. The index exists
    # in the DB; we don't need a Python-side Index() for the matcher to
    # use it (PostgreSQL planner picks it up automatically).
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
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
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
    # Remake vertical (CP-M10). Soft column, no FK (matches
    # parent_scenario_id). The composed final video of a remake carries
    # this so the publish/plan chain can trace it back.
    parent_remake_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # Versioning chain
    version: int = Field(default=1, sa_column=sa.Column(sa.Integer, nullable=False, server_default="1"))
    previous_version_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    replaced_by_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # Phase 3 — frame provenance. Set on rows extracted by the video
    # frame-extract worker. The director can offer the parent video's
    # keyframes as picks; compose can fall back to cutting the segment
    # around `source_timestamp_sec` from the parent video.
    source_asset_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    source_timestamp_sec: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True)
    )

    metadata_json: Optional[dict] = Field(default=None, sa_column=sa.Column("metadata", JSONB, nullable=True))

    # 'ready' | 'deleted' | 'expired'
    status: str = Field(default="ready", sa_column=sa.Column(sa.String(32), nullable=False))

    # ---- Brand asset library extension ----
    # When NULL, this row is a pipeline-produced intermediate (scene
    # image, voiceover, etc.) — not part of the reusable library.
    # When set, the matcher pulls this row into asset selection. See
    # the brand_asset_type taxonomy in PLAN; values are not enforced as
    # an enum so the taxonomy can grow without a migration.
    brand_asset_type: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.String(32), nullable=True)
    )
    # Vision-auto-tagged metadata. Free-form dict — see migration for
    # the documented shape (mood, dominant_colors, subjects, has_face,
    # motion_intensity, tags). Admins can edit values directly.
    brand_asset_tags: Optional[dict] = Field(
        default=None, sa_column=sa.Column(JSONB, nullable=True)
    )
    brand_kit_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.brand_kits.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    auto_tagged_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
