"""`social_accounts` — publishing accounts (distinct from ig_scraper.ig_accounts)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class SocialAccount(SQLModel, table=True):
    """A publishing account on Instagram or TikTok owned by the project."""

    __tablename__ = "social_accounts"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "provider", "handle", name="uq_social_accounts_project_provider_handle"),
        sa.Index("ix_social_accounts_project_id", "project_id"),
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

    # 'instagram' | 'tiktok'
    provider: str = Field(sa_column=sa.Column(sa.String(32), nullable=False))
    handle: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    external_account_id: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))

    # Encrypted Fernet blob containing tokens (access/refresh/expires_at JSON).
    credentials_encrypted: Optional[bytes] = Field(default=None, sa_column=sa.Column(sa.LargeBinary, nullable=True))

    # 'active' | 'expired' | 'revoked' | 'pending_oauth'
    status: str = Field(default="pending_oauth", sa_column=sa.Column(sa.String(32), nullable=False))

    last_used_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))

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
