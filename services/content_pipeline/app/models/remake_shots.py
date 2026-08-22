"""`remake_shots` — the per-shot creative plan of a remake.

One row per shot of the source video (shots come from
`video_frames.detect_scene_boundaries`). The planner assigns each shot
a `technique`; the operator edits it in plan_review. A shot's `status`
is derived by the reconciler from its `remake_steps`, never set by a
worker directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow

# copy   — verbatim cut, ffmpeg only, $0 (no branding visible)
# erase  — copy + video-inpaint the logo/watermark out
# restyle— video-to-video re-shoot with our brand refs (branding visible)
# reframe— keyframe edit (brand swap) + i2v re-animation (cheaper alt)
# drop   — omit the shot entirely
SHOT_TECHNIQUES = ("copy", "erase", "restyle", "reframe", "drop")

# planned    — authored, not yet approved / not yet started
# rendering  — at least one step running, none failed
# ready       — all steps succeeded, output_s3_key present
# needs_attention — a step exhausted retries
# dropped     — technique == drop
SHOT_STATUSES = ("planned", "rendering", "ready", "needs_attention", "dropped")


class RemakeShot(SQLModel, table=True):
    __tablename__ = "remake_shots"
    __table_args__ = (
        sa.UniqueConstraint("remake_id", "idx", name="uq_remake_shots_remake_idx"),
        sa.Index("ix_remake_shots_remake", "remake_id", "idx"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    remake_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.remakes.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    idx: int = Field(sa_column=sa.Column(sa.Integer, nullable=False))  # 0-based, source order
    start_sec: float = Field(sa_column=sa.Column(sa.Numeric(8, 3), nullable=False))
    end_sec: float = Field(sa_column=sa.Column(sa.Numeric(8, 3), nullable=False))

    technique: str = Field(
        default="copy", sa_column=sa.Column(sa.String(16), nullable=False, server_default="copy")
    )
    # Optional sub-range for 'copy' when only part of the shot is wanted.
    trim_start_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))
    trim_end_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))

    # Prompt for erase (what to remove) / restyle / reframe (what to make).
    prompt: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    # [{orig, replacement, t_start, t_end, position, style}] — on-screen
    # text replacements burned in at compose.
    text_plan: Optional[list] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    # {start, mid, end: s3_key} — keyframe thumbnails for the UI + vision.
    frames: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    # Stage-A vision output: {description, brand_visibility:{logos,products,
    # onscreen_text}, faces:{count,talking_head}, motion, setting}.
    tags: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    status: str = Field(
        default="planned", sa_column=sa.Column(sa.String(32), nullable=False, server_default="planned")
    )
    # The final NORMALIZED clip for this shot (fed straight to compose).
    output_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    # The clip's PROBED duration (not the planned window) — the cut is
    # re-encoded to a fixed fps and runs a frame or two long. Compose
    # windows captions + computes offsets from this so the timeline
    # stays in sync across many shots.
    output_duration_sec: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True)
    )

    est_cost_usd: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(10, 4), nullable=True))
    actual_cost_usd: float = Field(
        default=0.0, sa_column=sa.Column(sa.Numeric(10, 4), nullable=False, server_default="0")
    )
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
