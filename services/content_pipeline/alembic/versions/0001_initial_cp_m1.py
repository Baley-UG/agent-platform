"""CP-M1 initial — projects, brand_kits, social_accounts, content_references, templates,
music_tracks, media_assets, model_routes, generation_calls.

Revision ID: 0001_cp_m1
Revises:
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_cp_m1"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"


def upgrade() -> None:
    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("default_brand_kit_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("default_social_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reuse_policy", sa.String(16), nullable=False, server_default="warn"),
        sa.Column("weekly_budget_cap_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
        schema=SCHEMA,
    )

    # ------------------------------------------------------------------
    # brand_kits
    # ------------------------------------------------------------------
    op.create_table(
        "brand_kits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("logo_s3_key", sa.String(512), nullable=True),
        sa.Column("font_family", sa.String(255), nullable=True),
        sa.Column("primary_color", sa.String(16), nullable=True),
        sa.Column("secondary_color", sa.String(16), nullable=True),
        sa.Column("voice_id", sa.String(255), nullable=True),
        sa.Column("tts_lang", sa.String(16), nullable=True),
        sa.Column("tts_settings", postgresql.JSONB, nullable=True),
        sa.Column("style_prompt_suffix", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_brand_kits_project_id", "brand_kits", ["project_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # social_accounts
    # ------------------------------------------------------------------
    op.create_table(
        "social_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("handle", sa.String(255), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=True),
        sa.Column("credentials_encrypted", sa.LargeBinary, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_oauth"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id", "provider", "handle", name="uq_social_accounts_project_provider_handle"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_social_accounts_project_id", "social_accounts", ["project_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # content_references
    # ------------------------------------------------------------------
    op.create_table(
        "content_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_provider", sa.String(32), nullable=False),
        sa.Column("source_external_id", sa.String(255), nullable=True),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("media_s3_key", sa.String(512), nullable=True),
        sa.Column("poster_s3_key", sa.String(512), nullable=True),
        sa.Column("caption", sa.Text, nullable=True),
        sa.Column("transcript", sa.Text, nullable=True),
        sa.Column("hashtags", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("imported_by", sa.String(64), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_content_references_project_status_imported",
        "content_references",
        ["project_id", "status", "imported_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_content_references_project_provider_external",
        "content_references",
        ["project_id", "source_provider", "source_external_id"],
        schema=SCHEMA,
    )
    # Uniqueness only when source_external_id is present (manual_upload rows leave it NULL).
    op.execute(
        f'CREATE UNIQUE INDEX uq_content_references_external '
        f'ON "{SCHEMA}".content_references (project_id, source_provider, source_external_id) '
        f'WHERE source_external_id IS NOT NULL'
    )

    # ------------------------------------------------------------------
    # templates
    # ------------------------------------------------------------------
    op.create_table(
        "templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("video_s3_key", sa.String(512), nullable=True),
        sa.Column("duration_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("aspect_ratio", sa.String(16), nullable=True),
        sa.Column("insertion_rules", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_templates_project_id", "templates", ["project_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # music_tracks
    # ------------------------------------------------------------------
    op.create_table(
        "music_tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("audio_s3_key", sa.String(512), nullable=True),
        sa.Column("duration_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("bpm", sa.Integer, nullable=True),
        sa.Column("mood", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("license", sa.String(32), nullable=False, server_default="owned"),
        sa.Column("license_doc_url", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_music_tracks_project_id", "music_tracks", ["project_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # media_assets
    # ------------------------------------------------------------------
    op.create_table(
        "media_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("duration_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("parent_scenario_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_scene_idx", sa.Integer, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("previous_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="ready"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_media_assets_project_type", "media_assets", ["project_id", "type"], schema=SCHEMA)
    op.create_index("ix_media_assets_scenario", "media_assets", ["parent_scenario_id"], schema=SCHEMA)
    op.create_index("ix_media_assets_replaced_by", "media_assets", ["replaced_by_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # model_routes
    # ------------------------------------------------------------------
    op.create_table(
        "model_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("params", postgresql.JSONB, nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("cost_unit", sa.String(32), nullable=True),
        sa.Column("cost_per_unit_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("pricing_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_model_routes_resolve", "model_routes", ["project_id", "task_key", "priority"], schema=SCHEMA)
    # Two partial unique indexes — one for global (project_id IS NULL) rows, one for project-scoped.
    op.execute(
        f'CREATE UNIQUE INDEX uq_model_routes_global '
        f'ON "{SCHEMA}".model_routes (task_key, priority) '
        f'WHERE project_id IS NULL'
    )
    op.execute(
        f'CREATE UNIQUE INDEX uq_model_routes_scoped '
        f'ON "{SCHEMA}".model_routes (project_id, task_key, priority) '
        f'WHERE project_id IS NOT NULL'
    )

    # ------------------------------------------------------------------
    # generation_calls
    # ------------------------------------------------------------------
    op.create_table(
        "generation_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scene_idx", sa.Integer, nullable=True),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cached_tokens", sa.Integer, nullable=True),
        sa.Column("image_count", sa.Integer, nullable=True),
        sa.Column("video_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("audio_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("unit_count", sa.Integer, nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="success"),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_generation_calls_project_created",
        "generation_calls",
        ["project_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index("ix_generation_calls_scenario", "generation_calls", ["scenario_id"], schema=SCHEMA)
    op.create_index(
        "ix_generation_calls_provider_model",
        "generation_calls",
        ["provider", "model_id", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    for table in (
        "generation_calls",
        "model_routes",
        "media_assets",
        "music_tracks",
        "templates",
        "content_references",
        "social_accounts",
        "brand_kits",
        "projects",
    ):
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".{table} CASCADE')
    op.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
