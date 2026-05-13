"""`generation_calls` — per-API-call cost ledger.

One row per external provider call. Aggregations (per scenario, per
project, per week) roll up from this table. Provider clients write here
after every success or failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class GenerationCall(SQLModel, table=True):
    """A single external provider call (analyzer / image / video / TTS / etc.)."""

    __tablename__ = "generation_calls"
    __table_args__ = (
        sa.Index("ix_generation_calls_project_created", "project_id", "created_at"),
        sa.Index("ix_generation_calls_scenario", "scenario_id"),
        sa.Index("ix_generation_calls_provider_model", "provider", "model_id", "created_at"),
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

    scenario_id: Optional[uuid.UUID] = Field(default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True))
    scene_idx: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    variant_id: Optional[uuid.UUID] = Field(default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True))

    task_key: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))
    provider: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))
    model_id: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    request_id: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))

    # Usage counters — only the relevant fields are populated per provider.
    input_tokens: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    output_tokens: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    cached_tokens: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    image_count: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    video_seconds: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(10, 3), nullable=True))
    audio_seconds: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(10, 3), nullable=True))
    unit_count: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))

    cost_usd: float = Field(default=0.0, sa_column=sa.Column(sa.Numeric(12, 6), nullable=False, server_default="0"))

    # 'success' | 'failed' | 'timeout' | 'rate_limited'
    status: str = Field(default="success", sa_column=sa.Column(sa.String(32), nullable=False))
    latency_ms: Optional[int] = Field(default=None, sa_column=sa.Column(sa.Integer, nullable=True))
    error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
