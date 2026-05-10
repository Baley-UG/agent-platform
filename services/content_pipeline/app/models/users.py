"""`users` — admin panel login accounts.

Two global roles:
- `admin`  — manages users + projects + global model_routes
- `member` — operates projects they're assigned to via project_memberships

Per-project access is gated by `project_memberships`. A user with no
membership can't see a project even if they're a global admin (admins
self-add to projects via POST /projects/{pid}/members).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


GLOBAL_ROLES = ("admin", "member")
USER_STATUSES = ("active", "disabled")


class User(SQLModel, table=True):
    """One row per human admin-panel user."""

    __tablename__ = "users"
    __table_args__ = (
        sa.UniqueConstraint("email", name="uq_users_email"),
        {"schema": SCHEMA_NAME},
    )

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        sa_column=sa.Column(PGUUID(as_uuid=True), primary_key=True, nullable=False),
    )
    email: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    password_hash: str = Field(sa_column=sa.Column(sa.String(512), nullable=False))
    name: Optional[str] = Field(default=None, sa_column=sa.Column(sa.String(255), nullable=True))
    role: str = Field(default="member", sa_column=sa.Column(sa.String(32), nullable=False, server_default="member"))
    status: str = Field(default="active", sa_column=sa.Column(sa.String(32), nullable=False, server_default="active"))
    last_login_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))
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
