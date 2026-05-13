"""CP-M5 — render_variants + scenarios.voiceover_asset_id + scenarios.music_track_id.

Revision ID: 0006_cp_m5
Revises: 0005_cp_m4
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_cp_m5"
down_revision: Union[str, Sequence[str], None] = "0005_cp_m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    # ----- scenarios additions -----
    op.add_column(
        "scenarios",
        sa.Column("voiceover_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "scenarios",
        sa.Column("music_track_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_scenarios_voiceover_asset",
        "scenarios",
        "media_assets",
        ["voiceover_asset_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_scenarios_music_track",
        "scenarios",
        "music_tracks",
        ["music_track_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )

    # ----- render_variants table -----
    op.create_table(
        "render_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preset_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "final_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "thumbnail_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Multi-asset slot for variants that publish as IG / TT carousels
        # (multiple images, no single mp4). When NULL the variant has a
        # single `final_asset_id` (legacy video / single-photo). When set,
        # this list is authoritative and `final_asset_id` mirrors index 0.
        sa.Column("final_asset_ids", postgresql.JSONB, nullable=True),
        sa.Column("render_recipe", postgresql.JSONB, nullable=True),
        sa.Column("duration_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("scenario_id", "preset_key", name="uq_render_variants_scenario_preset"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_render_variants_scenario_status",
        "render_variants",
        ["scenario_id", "status"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".render_variants CASCADE')
    op.drop_constraint("fk_scenarios_music_track", "scenarios", schema=SCHEMA, type_="foreignkey")
    op.drop_constraint("fk_scenarios_voiceover_asset", "scenarios", schema=SCHEMA, type_="foreignkey")
    op.drop_column("scenarios", "music_track_id", schema=SCHEMA)
    op.drop_column("scenarios", "voiceover_asset_id", schema=SCHEMA)
