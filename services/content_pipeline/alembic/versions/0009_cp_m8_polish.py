"""CP-M6.5 + CP-M8 selective polish:
- scenarios.default_caption / default_hashtags
- plan_slots.caption_override / hashtags_override
- content_references.content_hash + caption_embedding (pgvector when available;
  falls back to bytea so the migration doesn't require the extension)
- references.curator_score / curator_reason for AI-curator output

Revision ID: 0009_cp_m8
Revises: 0008_cp_m7
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_cp_m8"
down_revision: Union[str, Sequence[str], None] = "0008_cp_m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    # Captions / hashtags on the publish path.
    op.add_column(
        "scenarios",
        sa.Column("default_caption", sa.Text, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "scenarios",
        sa.Column("default_hashtags", postgresql.ARRAY(sa.Text), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "plan_slots",
        sa.Column("caption_override", sa.Text, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "plan_slots",
        sa.Column("hashtags_override", postgresql.ARRAY(sa.Text), nullable=True),
        schema=SCHEMA,
    )

    # CP-Phase 2 — production mode (recreate | brand_build | inspire).
    # `recreate` keeps the existing analyzer + image_gen + ffmpeg path.
    # `brand_build` runs the director LLM against the brand asset
    # library. `inspire` skips visual production entirely.
    op.add_column(
        "scenarios",
        sa.Column(
            "production_mode",
            sa.String(16),
            nullable=False,
            server_default="recreate",
        ),
        schema=SCHEMA,
    )

    # Director-resolved brand asset per scene_render cell. NULL means
    # "fall through to AI synth" (image_gen worker generates the image).
    # `match_reason` is the LLM's short explanation, surfaced in the
    # panel so admin can sanity-check the director's picks.
    op.add_column(
        "scene_renders",
        sa.Column(
            "resolved_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.media_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.add_column(
        "scene_renders",
        sa.Column("match_reason", sa.Text, nullable=True),
        schema=SCHEMA,
    )

    # Phase 2.5 — per-cell img2img remix strength. 0 keeps the
    # resolved_asset_id as-is (pure passthrough); >0.15 routes through
    # fal's image-to-image endpoint with the resolved asset as the
    # init image. The director LLM proposes this value per scene
    # (low for product hero shots that should stay faithful, higher
    # for atmospheric scenes that can be remixed for variety).
    op.add_column(
        "scene_renders",
        sa.Column("image_strength", sa.Numeric(3, 2), nullable=True),
        schema=SCHEMA,
    )

    # Phase 4 — img2img-by-default. When the scenario is in
    # `recreate` mode (legacy AI-synth), the image_gen worker now
    # seeds each scene with the REFERENCE's matching frame:
    #   - photo source  → the single reference image (same for all)
    #   - carousel      → slide[scene_idx % len(slides)]
    #   - reel/video    → keyframe[scene_idx] extracted at import
    # The text prompt then describes the DELTA from that init frame
    # rather than from-scratch. `init_image_s3_key` is the S3 key
    # `image_gen` presigns at call time. Director-resolved cells
    # (Phase 2.5) use `resolved_asset_id` instead; the two paths are
    # mutually exclusive.
    op.add_column(
        "scene_renders",
        sa.Column("init_image_s3_key", sa.String(512), nullable=True),
        schema=SCHEMA,
    )

    # Reference dedup (CP-M8). Stored as bytea to avoid hard pgvector dep;
    # the perceptual hash compare is byte-distance so this is sufficient.
    # Embedding is reserved for future cosine similarity (admin opts in to
    # pgvector by re-typing this column to vector(1536) when ready).
    op.add_column(
        "content_references",
        sa.Column("content_hash", postgresql.BYTEA, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "content_references",
        sa.Column("caption_embedding", postgresql.BYTEA, nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "content_references",
        sa.Column("curator_score", sa.Numeric(4, 3), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "content_references",
        sa.Column("curator_reason", sa.Text, nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_content_references_content_hash",
        "content_references",
        ["project_id", "content_hash"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_content_references_content_hash", table_name="content_references", schema=SCHEMA)
    for col in ("curator_reason", "curator_score", "caption_embedding", "content_hash"):
        op.drop_column("content_references", col, schema=SCHEMA)
    for col in ("hashtags_override", "caption_override"):
        op.drop_column("plan_slots", col, schema=SCHEMA)
    for col in ("default_hashtags", "default_caption"):
        op.drop_column("scenarios", col, schema=SCHEMA)
