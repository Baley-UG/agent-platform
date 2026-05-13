"""`templates` — reusable video parts (intro/outro/lower_third/sticker/transition)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class Template(SQLModel, table=True):
    """A short video clip the compose stage can splice into final renders."""

    __tablename__ = "templates"
    __table_args__ = (
        sa.Index("ix_templates_project_id", "project_id"),
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
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))

    # 'intro' | 'outro' | 'lower_third' | 'sticker' | 'transition'
    kind: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))

    video_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    duration_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))
    aspect_ratio: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(16), nullable=True))

    insertion_rules: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
