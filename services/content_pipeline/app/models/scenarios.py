"""`scenarios` — the AI-generated production script for a piece of content.

A scenario is the bridge between a reference (raw material) and the rendered
variants. The analyzer fills `scenario_json` with scene breakdowns; the admin
edits/approves; image_gen / video_gen / audio_gen / compose pipelines consume
those scenes downstream.

Versioning of the scenario itself: when an admin regenerates the whole
`scenario_json`, we keep the prior payload in `previous_scenario_json` and
bump `version`. Per-scene asset versioning lives on `media_assets`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


# Status state machine — keep ordering stable; service code asserts transitions against it.
SCENARIO_STATUSES = (
    "draft",
    "analyzing",
    "pending_review",
    "approved",
    "generating_images",
    "images_ready",
    "generating_videos",
    "videos_ready",
    "generating_audio",
    "audio_ready",
    "composing",
    "final_pending_review",
    "approved_final",
    "failed",
)


class Scenario(SQLModel, table=True):
    """A production script. Sits between a `content_references` row and `render_variants`."""

    __tablename__ = "scenarios"
    __table_args__ = (
        sa.Index("ix_scenarios_project_status", "project_id", "status"),
        sa.Index("ix_scenarios_reference", "reference_id"),
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
    reference_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.content_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    status: str = Field(default="draft", sa_column=sa.Column(sa.String(32), nullable=False, server_default="draft"))

    # The core analyzer output. Shape documented in PLAN § 4.
    scenario_json: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))
    # Previous version retained for rollback after a full regenerate.
    previous_scenario_json: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))
    version: int = Field(default=1, sa_column=sa.Column(sa.Integer, nullable=False, server_default="1"))

    # Target render variants requested when the scenario was created.
    target_variants: Optional[List[str]] = Field(default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True))
    target_aspect_groups: Optional[List[str]] = Field(default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True))

    # 'draft' (cheap models) | 'final' (production models) — picks alternate model_route priorities.
    quality_tier: str = Field(default="final", sa_column=sa.Column(sa.String(16), nullable=False, server_default="final"))

    generation_cost_usd: float = Field(
        default=0.0, sa_column=sa.Column(sa.Numeric(12, 6), nullable=False, server_default="0")
    )

    # CP-M5 — currently active voiceover (versioned via media_assets chain) and
    # music selection (FK to a project's music_tracks library row).
    voiceover_asset_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    music_track_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # CP-M6.5 — default caption + hashtags used as the publisher fallback
    # when a plan_slot doesn't carry its own override.
    default_caption: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))
    default_hashtags: Optional[List[str]] = Field(
        default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True)
    )

    last_error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    created_by: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))
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
