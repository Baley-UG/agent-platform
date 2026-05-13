"""CP-M2 — reference_intake_rules, scenarios, reference_usages.

Revision ID: 0003_cp_m2
Revises: 0002_seed_model_routes
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_cp_m2"
down_revision: Union[str, Sequence[str], None] = "0002_seed_model_routes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # reference_intake_rules
    # ------------------------------------------------------------------
    op.create_table(
        "reference_intake_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("conditions", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("action", sa.String(32), nullable=False, server_default="queue_for_review"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_reference_intake_rules_project_priority",
        "reference_intake_rules",
        ["project_id", "priority"],
        schema=SCHEMA,
    )

    # ------------------------------------------------------------------
    # scenarios
    # ------------------------------------------------------------------
    op.create_table(
        "scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.content_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("scenario_json", postgresql.JSONB, nullable=True),
        sa.Column("previous_scenario_json", postgresql.JSONB, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("target_variants", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("target_aspect_groups", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("quality_tier", sa.String(16), nullable=False, server_default="final"),
        sa.Column("generation_cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_scenarios_project_status", "scenarios", ["project_id", "status"], schema=SCHEMA)
    op.create_index("ix_scenarios_reference", "scenarios", ["reference_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # reference_usages
    # ------------------------------------------------------------------
    op.create_table(
        "reference_usages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.content_references.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="produced"),
        sa.Column("reuse_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_reference_usages_reference", "reference_usages", ["reference_id", "created_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_reference_usages_project_created", "reference_usages", ["project_id", "created_at"], schema=SCHEMA
    )


def downgrade() -> None:
    for table in ("reference_usages", "scenarios", "reference_intake_rules"):
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".{table} CASCADE')
