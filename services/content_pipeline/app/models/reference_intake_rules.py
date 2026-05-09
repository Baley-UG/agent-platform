"""`reference_intake_rules` — rule-based filter that decides whether a freshly scraped
piece of content earns a `content_references` row automatically.

Each rule is a JSONB `conditions` object plus an `action` ('auto_import' or
`queue_for_review`). The matcher (`app.services.intake.match_against_rules`) walks
project-scoped rules in `priority` order and applies the first one that matches.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


class ReferenceIntakeRule(SQLModel, table=True):
    """A project-scoped rule that filters incoming scraped content."""

    __tablename__ = "reference_intake_rules"
    __table_args__ = (
        sa.Index("ix_reference_intake_rules_project_priority", "project_id", "priority"),
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
    name: str = Field(sa_column=sa.Column(sa.String(255), nullable=False))
    enabled: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.true()))

    conditions: dict = Field(default_factory=dict, sa_column=sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))

    # 'auto_import' | 'queue_for_review'
    action: str = Field(default="queue_for_review", sa_column=sa.Column(sa.String(32), nullable=False))
    priority: int = Field(default=0, sa_column=sa.Column(sa.Integer, nullable=False, server_default="0"))

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
