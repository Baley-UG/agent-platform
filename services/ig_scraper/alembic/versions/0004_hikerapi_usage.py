"""Per-request HikerAPI usage counter.

Adds `ig_hikerapi_usage` for real-time tracking of HikerAPI calls. Unlike
`ig_usage_daily` (keyed on `account_id` — irrelevant for HikerAPI which
doesn't use Instagram accounts), this table is keyed on (date, path) so
we can answer:

- "How many requests did we make today?"
- "Which endpoint is burning my quota?"
- "Did the rate-limit happen because of /v1/user/medias/chunk specifically?"

Upserted on every `HikerAPIClient.get(...)` so cost is visible mid-job,
not just at completion. `status_code` is denormalised into the key too
so success vs 4xx vs 5xx breakdowns are one-query.

Revision ID: 0004_hikerapi_usage
Revises: 0003_webhooks_deliveries
Create Date: 2026-05-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_hikerapi_usage"
down_revision: Union[str, Sequence[str], None] = "0003_webhooks_deliveries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ig_hikerapi_usage",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("path", sa.String(length=255), primary_key=True),
        sa.Column("status_code", sa.SmallInteger, primary_key=True),
        sa.Column("calls_total", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_ig_hikerapi_usage_date",
        "ig_hikerapi_usage",
        ["date"],
    )


def downgrade() -> None:
    op.drop_index("ix_ig_hikerapi_usage_date", table_name="ig_hikerapi_usage")
    op.drop_table("ig_hikerapi_usage")
