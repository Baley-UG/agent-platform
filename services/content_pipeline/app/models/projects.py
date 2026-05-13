"""`projects` — tenant root."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class Project(SQLModel, table=True):
    """A tenant. Owns brand kits, social accounts, references, plans, assets, budgets."""

    __tablename__ = "projects"
    # NOTE — `projects` lives in `public` (the platform-wide multi-tenancy
    # root). Other content_pipeline tables FK to `public.projects.id`
    # via a cross-schema reference. See migration 0001 for the
    # corresponding DDL.
    __table_args__ = (
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
        {"schema": "public"},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    slug: str = Field(sa_column=sa.Column(sa.String(64), nullable=False, unique=True))
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    status: str = Field(default="active", sa_column=sa.Column(sa.String(32), nullable=False))

    default_brand_kit_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    default_social_account_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # 'block' | 'warn' | 'silent'
    reuse_policy: str = Field(default="warn", sa_column=sa.Column(sa.String(16), nullable=False))
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
