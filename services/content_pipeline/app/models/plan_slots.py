"""`plan_slots` — one row per scheduled post.

Each slot has a target time, a target social account, a content type +
variant preset, and optional links to a render_variant (the asset to
publish) and a reference (when scenario generation is in flight to fill
this slot).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


PLAN_SLOT_STATUSES = (
    "empty",
    "filling",
    "ready",
    "scheduled",
    "publishing",
    "published",
    "failed",
    "skipped",
)
PLAN_SLOT_SOURCE_KINDS = ("stock", "scenario", "manual", "empty")
CONTENT_TYPES = ("post", "story", "reel", "tiktok_video")


class PlanSlot(SQLModel, table=True):
    """A single calendar slot inside a weekly_plan."""

    __tablename__ = "plan_slots"
    __table_args__ = (
        sa.Index(
            "ix_plan_slots_due",
            "scheduled_at",
            "status",
            postgresql_where=sa.text("status IN ('ready', 'scheduled')"),
        ),
        sa.Index("ix_plan_slots_plan", "weekly_plan_id"),
        sa.Index("ix_plan_slots_project", "project_id", "scheduled_at"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    weekly_plan_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.weekly_plans.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    project_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.projects.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    scheduled_at: datetime = Field(sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False))

    social_account_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.social_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    content_type: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))
    variant_preset: str = Field(sa_column=sa.Column(sa.String(64), nullable=False))

    source_kind: str = Field(default="empty", sa_column=sa.Column(sa.String(32), nullable=False, server_default="empty"))

    variant_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.render_variants.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    reference_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.content_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    status: str = Field(default="empty", sa_column=sa.Column(sa.String(32), nullable=False, server_default="empty"))

    suggested_variant_ids: Optional[List[uuid.UUID]] = Field(
        default=None,
        sa_column=sa.Column(ARRAY(PGUUID(as_uuid=True)), nullable=True),
    )

    publish_job_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    last_error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

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
