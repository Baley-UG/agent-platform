"""CP-M10 — remake vertical.

Adds the three remake tables (remakes / remake_shots / remake_steps),
the remake attribution columns on generation_calls + media_assets, and
the remake model_routes seeds. Drops the old scenario vertical
(render_variants / scene_renders / scenarios / reference_usages /
auto_generation_rules) and repoints plan_slots.variant_id at remakes.

APPEND-ONLY POLICY: this migration was written once and must never be
edited after it ships. (Revision 0009 was retro-edited and diverged
prod from dev — that must not recur. A CI check pins `alembic heads`
to a single head.)

Revision ID: 0012_cp_m10
Revises: 0011_cp_m9
Create Date: 2026-08-21
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_cp_m10"
down_revision: Union[str, Sequence[str], None] = "0011_cp_m9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"

# (task_key, provider, model_id, params, priority, cost_unit, cost_per_unit_usd)
SEEDS = [
    ("remake_shot_tag", "openrouter", "xiaomi/mimo-v2.5", {"temperature": 0.2, "max_tokens": 1200}, 0, "input_token", 0.000000119),
    ("remake_plan", "openrouter", "anthropic/claude-opus-5", {"temperature": 0.4, "max_tokens": 6000}, 0, "input_token", 0.000005),
    ("remake_plan", "openrouter", "google/gemini-3.6-flash", {"temperature": 0.4, "max_tokens": 6000}, 1, "input_token", 0.00000075),
    ("remake_asr", "fal", "fal-ai/whisper", {"chunk_level": "word"}, 0, "audio_second", 0.0000083),
    ("shot_erase", "fal", "fal-ai/void-video-inpainting", {}, 0, "unit", 0.05),
    ("shot_erase", "fal", "bria/video/erase/prompt", {}, 1, "video_second", 0.14),
    ("shot_restyle", "fal", "fal-ai/kling-video/o1/video-to-video/edit", {"keep_audio": True}, 0, "video_second", 0.168),
    ("shot_restyle", "fal", "decart/lucy-restyle", {}, 1, "video_second", 0.04),
    ("keyframe_edit", "fal", "fal-ai/nano-banana-pro/edit", {}, 0, "image", 0.15),
    ("shot_i2v", "fal", "fal-ai/kling-video/o3/standard/image-to-video", {}, 0, "video_second", 0.084),
    ("shot_i2v", "fal", "lightricks/ltx-2.5/image-to-video/pro", {}, 1, "video_second", 0.12),
    ("remake_tts", "elevenlabs", "eleven_multilingual_v2", {"stability": 0.5, "similarity_boost": 0.75}, 0, "input_token", 0.000180),
    ("remake_lipsync", "fal", "fal-ai/sync-lipsync/v2", {"model": "lipsync-2"}, 0, "audio_second", 0.05),
    ("remake_upscale", "fal", "topaz/upscale/video/precision", {}, 0, "video_second", 0.02),
]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ---- new attribution columns on shared tables ----
    op.add_column("generation_calls", sa.Column("remake_id", postgresql.UUID(as_uuid=True), nullable=True), schema=SCHEMA)
    op.add_column("generation_calls", sa.Column("remake_shot_id", postgresql.UUID(as_uuid=True), nullable=True), schema=SCHEMA)
    op.create_index("ix_generation_calls_remake", "generation_calls", ["remake_id"], schema=SCHEMA)
    op.add_column("media_assets", sa.Column("parent_remake_id", postgresql.UUID(as_uuid=True), nullable=True), schema=SCHEMA)

    # ---- remakes ----
    op.create_table(
        "remakes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("public.projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reference_id", postgresql.UUID(as_uuid=True), sa.ForeignKey(f"{SCHEMA}.content_references.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("brand_kit_id", postgresql.UUID(as_uuid=True), sa.ForeignKey(f"{SCHEMA}.brand_kits.id", ondelete="SET NULL"), nullable=True),
        sa.Column("preset_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="analyzing"),
        sa.Column("source_s3_key", sa.String(512), nullable=False),
        sa.Column("source_duration_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("source_meta", postgresql.JSONB, nullable=True),
        sa.Column("asr_json", postgresql.JSONB, nullable=True),
        sa.Column("plan_json", postgresql.JSONB, nullable=True),
        sa.Column("est_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("final_s3_key", sa.String(512), nullable=True),
        sa.Column("final_media_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_approved_by", sa.String(64), nullable=True),
        sa.Column("final_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_approved_by", sa.String(64), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("default_caption", sa.Text, nullable=True),
        sa.Column("default_hashtags", postgresql.ARRAY(sa.Text), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_remakes_project_status", "remakes", ["project_id", "status", "created_at"], schema=SCHEMA)

    # ---- remake_shots ----
    op.create_table(
        "remake_shots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("remake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey(f"{SCHEMA}.remakes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer, nullable=False),
        sa.Column("start_sec", sa.Numeric(8, 3), nullable=False),
        sa.Column("end_sec", sa.Numeric(8, 3), nullable=False),
        sa.Column("technique", sa.String(16), nullable=False, server_default="copy"),
        sa.Column("trim_start_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("trim_end_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("text_plan", postgresql.JSONB, nullable=True),
        sa.Column("frames", postgresql.JSONB, nullable=True),
        sa.Column("tags", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column("output_s3_key", sa.String(512), nullable=True),
        sa.Column("est_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("remake_id", "idx", name="uq_remake_shots_remake_idx"),
        schema=SCHEMA,
    )
    op.create_index("ix_remake_shots_remake", "remake_shots", ["remake_id", "idx"], schema=SCHEMA)

    # ---- remake_steps ----
    op.create_table(
        "remake_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("remake_id", postgresql.UUID(as_uuid=True), sa.ForeignKey(f"{SCHEMA}.remakes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("shot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey(f"{SCHEMA}.remake_shots.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="2"),
        sa.Column("input", postgresql.JSONB, nullable=True),
        sa.Column("output", postgresql.JSONB, nullable=True),
        sa.Column("est_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("generation_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema=SCHEMA,
    )
    op.create_index("ix_remake_steps_remake", "remake_steps", ["remake_id", "status"], schema=SCHEMA)
    op.create_index("ix_remake_steps_sweep", "remake_steps", ["status", "lease_expires_at"], schema=SCHEMA)

    # ---- repoint plan_slots.variant_id at remakes, then drop old tables ----
    # The old FK targeted render_variants (auto-named *_variant_id_fkey).
    op.execute(f'ALTER TABLE "{SCHEMA}".plan_slots DROP CONSTRAINT IF EXISTS plan_slots_variant_id_fkey')
    # Old pins reference now-deleted render_variants; null them so the new
    # FK is valid. (Operators re-pin from the remake stock view.)
    op.execute(f'UPDATE "{SCHEMA}".plan_slots SET variant_id = NULL WHERE variant_id IS NOT NULL')
    op.create_foreign_key(
        "plan_slots_variant_id_fkey", "plan_slots", "remakes",
        ["variant_id"], ["id"], source_schema=SCHEMA, referent_schema=SCHEMA, ondelete="SET NULL",
    )

    for table in ("render_variants", "scene_renders", "scenarios", "reference_usages", "auto_generation_rules"):
        op.execute(f'DROP TABLE IF EXISTS "{SCHEMA}".{table} CASCADE')

    # ---- seed remake model_routes ----
    insert_sql = sa.text(
        f'INSERT INTO "{SCHEMA}".model_routes '
        f"(id, project_id, task_key, provider, model_id, params, priority, enabled, "
        f" cost_unit, cost_per_unit_usd, pricing_updated_at, created_by) "
        f"VALUES (gen_random_uuid(), NULL, :task_key, :provider, :model_id, "
        f" CAST(:params AS jsonb), :priority, true, :cost_unit, :cost, now(), 'seed_m10')"
    )
    bind = op.get_bind()
    for task_key, provider, model_id, params, priority, cost_unit, cost in SEEDS:
        bind.execute(
            insert_sql,
            {
                "task_key": task_key, "provider": provider, "model_id": model_id,
                "params": json.dumps(params), "priority": priority,
                "cost_unit": cost_unit, "cost": cost,
            },
        )


def downgrade() -> None:
    # Irreversible: the dropped scenario tables cannot be reconstructed.
    # We only tear down what this migration ADDED, so a downgrade leaves a
    # working (scenario-less) DB rather than raising.
    op.execute(f"DELETE FROM \"{SCHEMA}\".model_routes WHERE created_by = 'seed_m10'")
    op.execute(f'ALTER TABLE "{SCHEMA}".plan_slots DROP CONSTRAINT IF EXISTS plan_slots_variant_id_fkey')
    op.drop_table("remake_steps", schema=SCHEMA)
    op.drop_table("remake_shots", schema=SCHEMA)
    op.drop_table("remakes", schema=SCHEMA)
    op.drop_column("media_assets", "parent_remake_id", schema=SCHEMA)
    op.drop_index("ix_generation_calls_remake", table_name="generation_calls", schema=SCHEMA)
    op.drop_column("generation_calls", "remake_shot_id", schema=SCHEMA)
    op.drop_column("generation_calls", "remake_id", schema=SCHEMA)
