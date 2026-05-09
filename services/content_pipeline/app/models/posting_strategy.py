"""`posting_strategy` — one row per project, holds the cadence rules.

Drives the weekly_plan skeleton generator (`preferred_slots × weekly_quota`)
and the auto-fill mode (manual / auto_suggest / auto_fill). Stored as a
single row per project rather than a small table because the structure is
fundamentally a project-level config.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


# Defaults applied at row create time.
DEFAULT_TIMEZONE = "Europe/Istanbul"
DEFAULT_WEEKLY_QUOTA = {"ig_reels": 5, "ig_story": 14, "ig_feed_45": 3, "tiktok": 7}
DEFAULT_PREFERRED_SLOTS = {
    "ig_reels": ["Mon 19:00", "Tue 12:00", "Thu 19:00", "Fri 21:00", "Sun 11:00"],
    "tiktok": ["daily 20:00"],
    "ig_story": ["daily 09:00", "daily 13:00"],
    "ig_feed_45": ["Wed 12:00", "Sat 12:00"],
}


class PostingStrategy(SQLModel, table=True):
    """One row per project. Created lazily on first read if missing."""

    __tablename__ = "posting_strategy"
    __table_args__ = (
        sa.UniqueConstraint("project_id", name="uq_posting_strategy_project"),
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

    timezone: str = Field(default=DEFAULT_TIMEZONE, sa_column=sa.Column(sa.String(64), nullable=False, server_default=DEFAULT_TIMEZONE))

    weekly_quota: dict = Field(
        default_factory=lambda: dict(DEFAULT_WEEKLY_QUOTA),
        sa_column=sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    preferred_slots: dict = Field(
        default_factory=lambda: dict(DEFAULT_PREFERRED_SLOTS),
        sa_column=sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    min_gap_minutes: dict = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    blackout: dict = Field(
        default_factory=dict,
        sa_column=sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )

    # 'manual' | 'auto_suggest' | 'auto_fill'
    fill_strategy: str = Field(default="auto_suggest", sa_column=sa.Column(sa.String(32), nullable=False, server_default="auto_suggest"))
    # 'off' | 'suggest' | 'auto'
    auto_generate_if_empty: str = Field(default="suggest", sa_column=sa.Column(sa.String(32), nullable=False, server_default="suggest"))

    approval_required_before_publish: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.true()))
    weekly_budget_cap_usd: Optional[float] = Field(default=None, sa_column=sa.Column(sa.Numeric(12, 2), nullable=True))

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
