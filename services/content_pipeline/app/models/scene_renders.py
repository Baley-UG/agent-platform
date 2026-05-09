"""`scene_renders` — master scene outputs keyed by (scenario, scene_idx, aspect_ratio).

Variants in the same aspect group share these masters: ig_reels and tiktok
both consume the 9:16 row; ig_feed_45 generates fresh in 4:5.

Per-scene asset versioning lives on `media_assets` (see `version` /
`replaced_by_id` chain). This row points at the *currently active* image and
video — when an admin regenerates a scene image, we update `image_asset_id`
to the new version and let the previous chain remain intact for rollback.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


# Per-scene state machine. The aggregate scenario state is derived from the
# rollup of these (see `scenarios_svc.recompute_status_from_renders`).
SCENE_RENDER_STATUSES = (
    "pending",
    "generating_image",
    "image_ready",
    "generating_video",
    "video_ready",
    "failed",
)


class SceneRender(SQLModel, table=True):
    """One per (scenario, scene_idx, aspect_ratio)."""

    __tablename__ = "scene_renders"
    __table_args__ = (
        sa.UniqueConstraint(
            "scenario_id", "scene_idx", "aspect_ratio", name="uq_scene_renders_scenario_scene_aspect"
        ),
        sa.Index("ix_scene_renders_scenario_status", "scenario_id", "status"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    scenario_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.scenarios.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    scene_idx: int = Field(sa_column=sa.Column(sa.Integer, nullable=False))
    aspect_ratio: str = Field(sa_column=sa.Column(sa.String(16), nullable=False))

    image_asset_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    video_asset_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    status: str = Field(default="pending", sa_column=sa.Column(sa.String(32), nullable=False, server_default="pending"))
    error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(
            sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )
