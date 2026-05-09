"""`model_routes` — central AI model registry.

One row per (project, task_key, priority). The `model_router.resolve()`
helper returns the lowest-priority enabled row, falling back through the
chain on provider failure. A row with `project_id IS NULL` is the global
default for that task; project-scoped rows shadow the global one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class ModelRoute(SQLModel, table=True):
    """Maps a logical task to a provider + model + params + pricing snapshot."""

    __tablename__ = "model_routes"
    __table_args__ = (
        sa.Index("ix_model_routes_resolve", "project_id", "task_key", "priority"),
        # Uniqueness handled via two partial indexes in the migration (NULL vs non-NULL project_id).
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    project_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    task_key: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))
    provider: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))
    model_id: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    params: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    priority: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    enabled: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.true()))

    # Pricing snapshot — providers stamp call cost from this row at call time.
    # 'input_token' | 'output_token' | 'image' | 'video_second' | 'audio_second' | 'call'
    cost_unit: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(32), nullable=True))
    cost_per_unit_usd: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(12, 8), nullable=True)
    )
    pricing_updated_at: Optional[datetime] = Field(
        default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True)
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
