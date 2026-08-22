"""CP-M10 hardening — actual cut duration on remake_shots.

The cut/normalize steps re-encode to a fixed fps, so a clip runs ~1-2
frames longer than its planned window. Compose was windowing captions
and computing offsets from the PLANNED window, which drifts
progressively across a multi-shot video. Persist the probed output
duration so compose uses the real timeline.

Revision ID: 0013_cp_m10_hardening
Revises: 0012_cp_m10
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_cp_m10_hardening"
down_revision: Union[str, Sequence[str], None] = "0012_cp_m10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.add_column(
        "remake_shots",
        sa.Column("output_duration_sec", sa.Numeric(8, 3), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("remake_shots", "output_duration_sec", schema=SCHEMA)
