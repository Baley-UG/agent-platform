"""`project` — platform-wide multi-tenancy root.

Lives in `public` schema. The CRUD lives here in main app since the
project entity isn't content_pipeline-specific — ig_scraper, future
TikTok service, and any other downstream all key resources off
`project_id`. Sub-resources (brand_kits, scenarios, plan_slots, etc.)
remain owned by their respective service's tables and are mounted
under `/cp/projects/{pid}/...` via the gateway.

Mirrors the column set originally defined in
`services/content_pipeline/app/models/projects.py` so existing rows
read cleanly through this model. Migration history is preserved (the
table was renamed via `ALTER TABLE ... SET SCHEMA public`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.utcnow()


class Project(SQLModel, table=True):
    """A tenant. Owns brand kits, social accounts, references, plans, assets, budgets."""

    __tablename__ = "projects"
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
    # 'active' | 'archived' (soft delete) | 'paused'
    status: str = Field(default="active", sa_column=sa.Column(sa.String(32), nullable=False))

    default_brand_kit_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )
    default_social_account_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=sa.Column(PGUUID(as_uuid=True), nullable=True)
    )

    # 'block' | 'warn' | 'silent' — see content_pipeline's reuse logic.
    reuse_policy: str = Field(default="warn", sa_column=sa.Column(sa.String(16), nullable=False))
    weekly_budget_cap_usd: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(12, 2), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=sa.Column(
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
