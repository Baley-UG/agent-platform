"""`reference_usages` — every time a reference is used to spawn a scenario, we
log a row here so the reuse-policy gate can answer "how often have we
reproduced this?" without a JOIN against scenarios.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class ReferenceUsage(SQLModel, table=True):
    """Audit row for one (reference, scenario) pairing."""

    __tablename__ = "reference_usages"
    __table_args__ = (
        sa.Index("ix_reference_usages_reference", "reference_id", "created_at"),
        sa.Index("ix_reference_usages_project_created", "project_id", "created_at"),
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
    reference_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.content_references.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    scenario_id: uuid.UUID = Field(
        sa_column=sa.Column(
            PGUUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA_NAME}.scenarios.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # 'produced' (scenario exists) | 'published' (it shipped) | 'abandoned' (user discarded).
    status: str = Field(default="produced", sa_column=sa.Column(sa.String(32), nullable=False, server_default="produced"))

    reuse_reason: str = Field(default="", sa_column=sa.Column(sa.Text, nullable=False, server_default=""))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
