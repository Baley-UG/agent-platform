"""`weekly_plans` — one row per (project, week_start). Holds the skeleton."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


WEEKLY_PLAN_STATUSES = ("draft", "approved", "active", "archived")


class WeeklyPlan(SQLModel, table=True):
    """A week of planned slots for a project. Materialized at generate-time."""

    __tablename__ = "weekly_plans"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "week_start_date", name="uq_weekly_plans_project_week"),
        sa.Index("ix_weekly_plans_project", "project_id", "week_start_date"),
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
    # Monday of the ISO week (UTC date).
    week_start_date: date = Field(sa_column=sa.Column(sa.Date, nullable=False))

    # 'draft' | 'approved' | 'active' | 'archived'
    status: str = Field(default="draft", sa_column=sa.Column(sa.String(32), nullable=False, server_default="draft"))

    generated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    generated_by: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))
    notes: Optional[str] = Field(default=None, sa_column=sa.Column(sa.Text, nullable=True))

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
