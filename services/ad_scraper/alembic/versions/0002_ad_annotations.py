"""AI annotations on ad_materials — script + summary.

`script` is the video's scenario (shot-by-shot description of what
happens), `summary` a short abstract. Neither comes from the upstream
API — they are authored by an agent that watched the creative, via the
MCP `annotate_ad` tool, so the library becomes searchable by what the
ads actually DO rather than only their slogan/ASR.

Revision ID: 0002_ad_annotations
Revises: 0001_initial_ad_m1
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ad_annotations"
down_revision: Union[str, Sequence[str], None] = "0001_initial_ad_m1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ad_materials", sa.Column("script", sa.Text(), nullable=True))
    op.add_column("ad_materials", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "ad_materials", sa.Column("annotated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ad_materials", "annotated_at")
    op.drop_column("ad_materials", "summary")
    op.drop_column("ad_materials", "script")
