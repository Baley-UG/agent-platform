"""`auth_sessions` — refresh-token store, revokable.

Access tokens are stateless JWTs (1h). Refresh tokens are random opaque
strings; the hash lives here so we can revoke individual sessions
(logout, "sign out other devices", admin disable).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class AuthSession(SQLModel, table=True):
    """One row per active refresh token."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        sa.Index("ix_auth_sessions_user", "user_id"),
        sa.Index("ix_auth_sessions_token_hash", "token_hash"),
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
    token_hash: str = Field(sa_column=sa.Column(sa.String(128), nullable=False))
    expires_at: datetime = Field(sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False))
    last_used_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))
    user_agent: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(512), nullable=True))
    ip: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(64), nullable=True))
    revoked_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
