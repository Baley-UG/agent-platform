"""`project_memberships` — user × project with a per-project role.

Per-project roles:
- `owner`  — every action including delete project, manage members
- `editor` — scenarios, plans, publish; cannot edit posting_strategy /
             model_routes / brand_kits / social_accounts
- `viewer` — read only

A global `admin` user can self-add to any project (the membership row
still has to exist; we don't auto-shortcut so audit trails stay clean
when CP-M9 adds them).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


PROJECT_ROLES = ("owner", "editor", "viewer")


class ProjectMembership(SQLModel, table=True):
    """One row per (user, project) pair."""

    __tablename__ = "project_memberships"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "project_id", name="uq_project_memberships_user_project"),
        sa.Index("ix_project_memberships_user", "user_id"),
        sa.Index("ix_project_memberships_project", "project_id"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    user_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.users.id", ondelete="CASCADE"),
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
    role: str = Field(default="editor", sa_column=sa.Column(sa.String(32), nullable=False, server_default="editor"))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
