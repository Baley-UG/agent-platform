"""`publish_jobs` — one row per publish attempt to IG / TikTok.

Holds the provider-side container/publish ids so we can poll status,
retry on failure, and reconcile after a worker crash.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


PUBLISH_JOB_STATUSES = ("pending", "uploading", "processing", "published", "failed")


class PublishJob(SQLModel, table=True):
    """One publish attempt. May fail and retry → multiple rows per slot."""

    __tablename__ = "publish_jobs"
    __table_args__ = (
        sa.Index("ix_publish_jobs_slot", "plan_slot_id"),
        sa.Index("ix_publish_jobs_status", "status", "created_at"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    plan_slot_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.plan_slots.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    social_account_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.social_accounts.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    provider: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))

    provider_container_id: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.String(255), nullable=True)
    )
    provider_media_id: Optional[str] = Field(
        default=None, sa_column=sa.Column(sa.String(255), nullable=True)
    )

    status: str = Field(default="pending", sa_column=sa.Column(sa.String(32), nullable=False, server_default="pending"))

    attempts: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))
    last_error: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))
    response: Optional[dict] = Field(default=None, sa_column=sa.Column(JSONB, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    published_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))
