"""`remake_steps` — the execution graph of a remake.

One row per unit of work. This table is what makes the pipeline a job
GRAPH instead of v1's fragile chain-of-enqueues: every step declares its
`kind` and `seq`, the reconciler enqueues a step only once every earlier
step in its scope has succeeded, and a periodic sweep re-drives anything
whose lease expired. Nothing is enqueued from inside a worker except the
`advance()` call at the end.

Scopes:
  - shot_id set  → a per-shot step (cut, erase, restyle, keyframe_edit,
                   i2v, normalize)
  - shot_id NULL → a remake-global step (probe, scene_detect,
                   frame_extract, asr, tag_shots, author_plan, tts,
                   lipsync, compose, upscale)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow

STEP_STATUSES = ("pending", "queued", "running", "succeeded", "failed", "skipped")

# Which queue each kind runs on — mirrored in the reconciler's dispatch
# map. ffmpeg kinds must land on the render container.
STEP_QUEUES = {
    # analysis
    "probe": "remake_ffmpeg",
    "scene_detect": "remake_ffmpeg",
    "frame_extract": "remake_ffmpeg",
    "asr": "remake_ai",
    "tag_shots": "remake_analysis",
    "author_plan": "remake_analysis",
    # per-shot
    "cut": "remake_ffmpeg",
    "erase": "remake_ai",
    "restyle": "remake_ai",
    "keyframe_edit_start": "remake_ai",
    "keyframe_edit_end": "remake_ai",
    "i2v": "remake_ai",
    "normalize": "remake_ffmpeg",
    # render-global
    "tts": "remake_ai",
    "lipsync": "remake_ai",
    "compose": "remake_ffmpeg",
    "upscale": "remake_ai",
}


class RemakeStep(SQLModel, table=True):
    __tablename__ = "remake_steps"
    __table_args__ = (
        sa.Index("ix_remake_steps_remake", "remake_id", "status"),
        sa.Index("ix_remake_steps_sweep", "status", "lease_expires_at"),
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
    shot_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.remake_shots.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    kind: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))
    seq: int = Field(sa_column=sa.Column(sa.Integer, nullable=False))

    status: str = Field(
        default="pending", sa_column=sa.Column(sa.String(16), nullable=False, server_default="pending")
    )
    attempts: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    max_attempts: int = Field(default=2, sa_column=sa.Column(sa.Integer, nullable=False, server_default="2"))

    # Resolved at author time (s3 keys, prompt, durations, ref keys).
    input: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))
    # {s3_key, provider_request_id, duration_sec, ...}.
    output: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    est_cost_usd: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(10, 4), nullable=True))
    generation_call_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # Set on enqueue; the sweep re-drives a step whose lease elapsed
    # (worker crashed / dropped message) without waiting on RQ.
    lease_expires_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
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
