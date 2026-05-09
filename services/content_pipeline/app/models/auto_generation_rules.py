"""`auto_generation_rules` — proactive scenario auto-generation per project.

The hourly auto-generation loop walks enabled rules, checks each against
its `daily_quota` (already-generated scenarios today) and the project's
weekly_budget_cap_usd, and enqueues new scenarios from the highest-scoring
candidate references.

This is the proactive variant of the reactive `auto_generate_if_empty`
flag on `posting_strategy`. Both can run side-by-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models._base import SCHEMA_NAME, utcnow


PICK_STRATEGIES = ("highest_score", "newest", "diverse")


class AutoGenerationRule(SQLModel, table=True):
    """One row per project + named rule. A project can have multiple rules
    (e.g. one for Reels, one for Stories) with distinct quality_tiers
    and target_variants.
    """

    __tablename__ = "auto_generation_rules"
    __table_args__ = (
        sa.Index("ix_auto_gen_rules_project_enabled", "project_id", "enabled"),
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

    # 'highest_score' (M8 score) | 'newest' (imported_at desc) | 'diverse' (round-robin authors)
    pick_strategy: str = Field(
        default="highest_score", sa_column=sa.Column(sa.String(32), nullable=False, server_default="highest_score")
    )

    daily_quota: int = Field(default=1, sa_column=sa.Column(sa.Integer, nullable=False, server_default="1"))

    target_variants: Optional[List[str]] = Field(
        default=None, sa_column=sa.Column(ARRAY(sa.Text), nullable=True)
    )
    quality_tier: str = Field(default="final", sa_column=sa.Column(sa.String(16), nullable=False, server_default="final"))

    budget_cap_usd: Optional[float] = Field(
        default=None, sa_column=sa.Column(sa.Numeric(12, 2), nullable=True)
    )

    last_run_at: Optional[datetime] = Field(default=None, sa_column=sa.Column(sa.DateTime(timezone=True), nullable=True))

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
