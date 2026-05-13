"""CP-M7 — auto_generation_rules.

Revision ID: 0008_cp_m7
Revises: 0007_cp_m6
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_cp_m7"
down_revision: Union[str, Sequence[str], None] = "0007_cp_m6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.create_table(
        "auto_generation_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("pick_strategy", sa.String(32), nullable=False, server_default="highest_score"),
        sa.Column("daily_quota", sa.Integer, nullable=False, server_default="1"),
        sa.Column("target_variants", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("quality_tier", sa.String(16), nullable=False, server_default="final"),
        sa.Column("budget_cap_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_auto_gen_rules_project_enabled",
        "auto_generation_rules",
        ["project_id", "enabled"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".auto_generation_rules CASCADE')
