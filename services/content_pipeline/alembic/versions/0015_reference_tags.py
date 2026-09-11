"""content_references.tags — curated labels, one-click filters in the panel.

Distinct from `hashtags` (scraped from the source post): tags are
operator-assigned taxonomy ("hook", "ugc", "q4-campaign", ...).

Revision ID: 0015_reference_tags
Revises: 0014_reference_title
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0015_reference_tags"
down_revision: Union[str, Sequence[str], None] = "0014_reference_title"
branch_labels = None
depends_on = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.add_column(
        "content_references",
        sa.Column("tags", ARRAY(sa.Text()), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("content_references", "tags", schema=SCHEMA)
