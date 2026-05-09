"""`render_variants` — one row per scenario × variant_preset (ig_reels, tiktok, ig_feed_45, …).

The compose stage produces one final video per row. Variants in the same
aspect group reuse the underlying scene videos; their differences are at
the compose layer (safe-zone-aware text, platform LUFS, max-duration cuts).

Per-variant final asset versioning lives on `media_assets` via the
`(version, replaced_by_id)` chain. `final_asset_id` always points at the
currently-active version.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


RENDER_VARIANT_STATUSES = (
    "pending",
    "composing",
    "ready",
    "approved",
    "published",
    "failed",
)


class RenderVariant(SQLModel, table=True):
    """One row per (scenario, variant_preset). Final video output target."""

    __tablename__ = "render_variants"
    __table_args__ = (
        sa.UniqueConstraint("scenario_id", "preset_key", name="uq_render_variants_scenario_preset"),
        sa.Index("ix_render_variants_scenario_status", "scenario_id", "status"),
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
    preset_key: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))

    status: str = Field(default="pending", sa_column=sa.Column(sa.String(32), nullable=False, server_default="pending"))

    final_asset_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    thumbnail_asset_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # idempotent regen: ffmpeg args / input list / preset snapshot at the
    # time the variant was last composed. Re-running the same render with
    # the same recipe should byte-equal-ish.
    render_recipe: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    duration_sec: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(8, 3), nullable=True))
    file_size_bytes: Optional[int] = Field(default=None, sa_column=sa.Column(sa.BigInteger, nullable=True))

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
    approved_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))
