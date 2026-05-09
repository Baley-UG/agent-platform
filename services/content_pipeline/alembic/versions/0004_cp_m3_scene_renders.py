"""CP-M3 — scene_renders.

Revision ID: 0004_cp_m3
Revises: 0003_cp_m2
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_cp_m3"
down_revision: Union[str, Sequence[str], None] = "0003_cp_m2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.create_table(
        "scene_renders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scene_idx", sa.Integer, nullable=False),
        sa.Column("aspect_ratio", sa.String(16), nullable=False),
        sa.Column(
            "image_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "video_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "scenario_id", "scene_idx", "aspect_ratio", name="uq_scene_renders_scenario_scene_aspect"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_scene_renders_scenario_status",
        "scene_renders",
        ["scenario_id", "status"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".scene_renders CASCADE')
