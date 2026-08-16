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

    # CP-Phase 2 — director's resolved brand asset for this cell.
    # When set, the image_gen worker SKIPS synthesis and treats the
    # resolved asset as if image_gen had produced it. NULL means
    # "AI fallback" (legacy text2img / img2img path).
    resolved_asset_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Short text explaining why the director picked this asset — useful
    # for the panel's "why this asset?" tooltip + admin sanity-check.
    match_reason: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.Text, nullable=True)
    )
    # Phase 2.5 — img2img remix strength when resolved_asset_id is set.
    # 0..0.15 = pure passthrough (no LLM call). 0.16..0.6 = light remix
    # (preserves composition). 0.6..1.0 = heavy remix (free-er prompt
    # adherence, original used loosely as composition guide). NULL means
    # "use default from model_routes.params.image_strength".
    image_strength: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(3, 2), nullable=True)
    )
    # Phase 4 — img2img-by-default. The reference's matching frame
    # (photo / carousel slide / reel keyframe) seeded onto this cell at
    # materialize time. image_gen presigns this key as the init image
    # for fal's `/image-to-image` endpoint when `resolved_asset_id` is
    # NOT set. Stored as a plain S3 key (not a media_assets ref) since
    # reference frames are managed under content_references, not the
    # media_assets versioning chain.
    init_image_s3_key: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.String(512), nullable=True)
    )

    # repurpose mode — the source-video window this cell was cut from.
    # Mirrors the matching `scenarios.segment_plan.segments[]` entry so a
    # single render can be re-cut without re-reading the whole plan.
    # `segment_action` ∈ {keep, replace, drop}: `keep` cuts real footage,
    # `replace` falls through to the existing image_gen/video_gen path.
    source_start_sec: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True)
    )
    source_end_sec: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True)
    )
    segment_action: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.String(16), nullable=True)
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
