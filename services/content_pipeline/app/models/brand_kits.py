"""`brand_kits` — colors, fonts, logos, voice."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class BrandKit(SQLModel, table=True):
    """Project-scoped brand asset bundle."""

    __tablename__ = "brand_kits"
    __table_args__ = (
        sa.Index("ix_brand_kits_project_id", "project_id"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    project_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.projects.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    is_default: bool = Field(default=False, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.false()))

    logo_s3_key: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    font_family: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))
    primary_color: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(16), nullable=True))
    secondary_color: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(16), nullable=True))

    voice_id: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))
    tts_lang: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(16), nullable=True))
    tts_settings: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    style_prompt_suffix: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

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
