"""`remakes` — one competitor-ad remake order.

The remake vertical (CP-M10) replaces the old scenario pipeline. A
remake takes ONE `content_references` row (an ad_scraper creative or an
ig_scraper reel, both already mirrored to S3) and produces a rebranded
near-copy of it: shots where no competitor branding is visible are
copied verbatim; shots with a logo are erased or AI-reshot.

Three tables, one responsibility each — this is deliberate, to avoid
the v1 mistake of overloading `scene_renders` with four meanings:

  - `remakes`       — the order + global creative decisions + status
  - `remake_shots`  — the per-shot creative plan (technique, prompt)
  - `remake_steps`  — the execution graph (one row per unit of work)

Status is derived by the reconciler (`app/services/remake_reconciler.py`)
from the child rows, never advanced ad-hoc by workers. Only six states
are ever shown to the operator.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow

# Operator-visible status set — exhaustive. `needs_attention` is a flag
# state: a step exhausted its retries, but healthy shots keep rendering.
REMAKE_STATUSES = (
    "analyzing",       # creation → analysis steps running
    "plan_review",     # Gate 1 — plan authored, awaiting human approval
    "rendering",       # Gate 1 approved; render steps running (x/y shots)
    "needs_attention", # a step failed after max_attempts; siblings continue
    "final_review",    # Gate 2 — composed video awaiting human approval
    "done",            # Gate 2 approved; media_asset created, publishable
    "archived",
)

# Human gates + terminal states the reconciler must never cross on its own.
REMAKE_FROZEN_STATUSES = ("plan_review", "final_review", "done", "archived")


class Remake(SQLModel, table=True):
    __tablename__ = "remakes"
    __table_args__ = (
        sa.Index("ix_remakes_project_status", "project_id", "status", "created_at"),
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
    reference_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.content_references.id", ondelete="RESTRICT"),
            nullable=False,
        )
    )
    brand_kit_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.brand_kits.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    preset_key: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))
    status: str = Field(
        default="analyzing",
        sa_column=sa.Column(sa.String(32), nullable=False, server_default="analyzing"),
    )

    # Copied from `content_references.media_s3_key` at creation so the
    # remake is stable even if the reference is later re-mirrored.
    source_s3_key: str = Field(sa_column=sa.Column(sa.String(512), nullable=False))
    source_duration_sec: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True)
    )
    source_meta: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    # fal whisper output: full text + word-level chunks + diarization.
    asr_json: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    # Global creative decisions authored by the planner and editable in
    # plan_review: {audio_mode, voice_script, cta_text, outro_template_id,
    # logo_overlay:{position,scale,opacity}, notes, brand_findings}.
    plan_json: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    est_cost_usd: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(10, 4), nullable=True)
    )
    actual_cost_usd: float = Field(
        default=0.0, sa_column=sa.Column(sa.Numeric(10, 4), nullable=False, server_default="0")
    )

    # Composed output. `final_s3_key` is the raw compose result (versioned
    # so a shot-reject recompose never overwrites what the reviewer is
    # watching); `final_media_asset_id` is stamped on Gate-2 approval and
    # is what the publish/plan chain consumes.
    final_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    final_media_asset_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    plan_approved_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )
    plan_approved_by: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))
    final_approved_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
    )
    final_approved_by: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))

    # Only set for analysis-stage hard failures (before any shots exist).
    error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    # Publish-text fallback (seeded from the reference caption), same
    # role `scenarios.default_caption` played — captions.resolve reads it.
    default_caption: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))
    default_hashtags: Optional[list] = Field(
        default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True)
    )

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
