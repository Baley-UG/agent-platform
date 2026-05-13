"""CP-M6 — posting_strategy + weekly_plans + plan_slots + publish_jobs.

Revision ID: 0007_cp_m6
Revises: 0006_cp_m5
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_cp_m6"
down_revision: Union[str, Sequence[str], None] = "0006_cp_m5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    # ----- posting_strategy -----
    op.create_table(
        "posting_strategy",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Europe/Istanbul"),
        sa.Column("weekly_quota", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("preferred_slots", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("min_gap_minutes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("blackout", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fill_strategy", sa.String(32), nullable=False, server_default="auto_suggest"),
        sa.Column("auto_generate_if_empty", sa.String(32), nullable=False, server_default="suggest"),
        sa.Column("approval_required_before_publish", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("weekly_budget_cap_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", name="uq_posting_strategy_project"),
        schema=SCHEMA,
    )

    # ----- weekly_plans -----
    op.create_table(
        "weekly_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("week_start_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("generated_by", sa.String(64), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "week_start_date", name="uq_weekly_plans_project_week"),
        schema=SCHEMA,
    )
    op.create_index("ix_weekly_plans_project", "weekly_plans", ["project_id", "week_start_date"], schema=SCHEMA)

    # ----- plan_slots -----
    op.create_table(
        "plan_slots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "weekly_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.weekly_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("public.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "social_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.social_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("variant_preset", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False, server_default="empty"),
        sa.Column(
            "variant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.render_variants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.content_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="empty"),
        sa.Column("suggested_variant_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
        sa.Column("publish_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_plan_slots_plan", "plan_slots", ["weekly_plan_id"], schema=SCHEMA)
    op.create_index("ix_plan_slots_project", "plan_slots", ["project_id", "scheduled_at"], schema=SCHEMA)
    op.execute(
        f'CREATE INDEX ix_plan_slots_due ON "{SCHEMA}".plan_slots (scheduled_at, status) '
        f"WHERE status IN ('ready', 'scheduled')"
    )

    # ----- publish_jobs -----
    op.create_table(
        "publish_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "plan_slot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.plan_slots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "social_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.social_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_container_id", sa.String(255), nullable=True),
        sa.Column("provider_media_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("response", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index("ix_publish_jobs_slot", "publish_jobs", ["plan_slot_id"], schema=SCHEMA)
    op.create_index("ix_publish_jobs_status", "publish_jobs", ["status", "created_at"], schema=SCHEMA)


def downgrade() -> None:
    for table in ("publish_jobs", "plan_slots", "weekly_plans", "posting_strategy"):
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".{table} CASCADE')
