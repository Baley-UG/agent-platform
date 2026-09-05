"""Operator-facing title on content_references.

References carried only the source's own `caption` (the competitor's ad
copy) — nothing let the operator or an agent NAME a reference video for
their own bookkeeping ("Rakip X — before/after hook"). Nullable; falls
back to caption everywhere it's displayed.

Revision ID: 0014_reference_title
Revises: 0013_cp_m10_hardening
Create Date: 2026-09-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_reference_title"
down_revision: Union[str, Sequence[str], None] = "0013_cp_m10_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.add_column(
        "content_references",
        sa.Column("title", sa.String(255), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("content_references", "title", schema=SCHEMA)
