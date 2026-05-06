"""Webhook delivery tracking + per-account warmup column.

Two unrelated additions bundled into one migration so M9+M10 share a
single schema bump:

- `ig_webhook_deliveries`: per-attempt log for outbound webhook calls.
  Lets the dispatcher do retry/backoff with full visibility for ops.
- `ig_accounts.onboarded_at`: timestamps when an account was first
  promoted from `fresh` quota tier so the warm-up auto-promotion
  scheduler can flip it to `mid` after IG_WARMUP_HOURS.

Revision ID: 0003_webhooks_deliveries
Revises: 0002_scoring_views
Create Date: 2026-05-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_webhooks_deliveries"
down_revision: Union[str, Sequence[str], None] = "0002_scoring_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ig_webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "webhook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ig_webhooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_ig_webhook_deliveries_status_scheduled",
        "ig_webhook_deliveries",
        ["status", "scheduled_for"],
    )
    op.create_index(
        "ix_ig_webhook_deliveries_webhook",
        "ig_webhook_deliveries",
        ["webhook_id"],
    )

    # M10: stamp when an account first started being used so we can
    # safely auto-promote fresh -> mid after IG_WARMUP_HOURS.
    op.add_column(
        "ig_accounts",
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ig_accounts", "onboarded_at")
    op.drop_index("ix_ig_webhook_deliveries_webhook", table_name="ig_webhook_deliveries")
    op.drop_index(
        "ix_ig_webhook_deliveries_status_scheduled", table_name="ig_webhook_deliveries"
    )
    op.drop_table("ig_webhook_deliveries")
