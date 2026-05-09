"""Seed global model_routes defaults.

These are the project_id IS NULL rows that act as fallbacks when a project
hasn't customized a task_key. Admins can override per-project at any time
via the API. Pricing snapshots reflect representative public prices at
seed time and should be refreshed by ops as providers change them.

Revision ID: 0002_seed_model_routes
Revises: 0001_cp_m1
Create Date: 2026-05-09
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_model_routes"
down_revision: Union[str, Sequence[str], None] = "0001_cp_m1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"

# (task_key, provider, model_id, params, priority, cost_unit, cost_per_unit_usd)
SEEDS = [
    (
        "scenario_analysis",
        "openrouter",
        "anthropic/claude-sonnet-4.6",
        {"temperature": 0.4, "max_tokens": 4000},
        0,
        "input_token",
        0.0000030,
    ),
    (
        "scene_image",
        "fal",
        "fal-ai/flux/dev",
        {"image_size": "portrait_16_9", "num_inference_steps": 28},
        0,
        "image",
        0.025,
    ),
    (
        "scene_video",
        "seedance",
        "seedance-v1-pro-i2v",
        {"duration_sec": 5},
        0,
        "video_second",
        0.10,
    ),
    (
        "voiceover_tts",
        "elevenlabs",
        "eleven_multilingual_v2",
        {"stability": 0.5, "similarity_boost": 0.75},
        0,
        "input_token",
        0.000180,
    ),
]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    insert_sql = sa.text(
        f'INSERT INTO "{SCHEMA}".model_routes '
        f"(id, project_id, task_key, provider, model_id, params, priority, enabled, "
        f" cost_unit, cost_per_unit_usd, pricing_updated_at, created_by) "
        f"VALUES (gen_random_uuid(), NULL, :task_key, :provider, :model_id, "
        f" CAST(:params AS jsonb), :priority, true, :cost_unit, :cost, "
        f" now(), 'seed')"
    )
    bind = op.get_bind()
    for task_key, provider, model_id, params, priority, cost_unit, cost in SEEDS:
        bind.execute(
            insert_sql,
            {
                "task_key": task_key,
                "provider": provider,
                "model_id": model_id,
                "params": json.dumps(params),
                "priority": priority,
                "cost_unit": cost_unit,
                "cost": cost,
            },
        )


def downgrade() -> None:
    op.execute(
        f'DELETE FROM "{SCHEMA}".model_routes WHERE created_by = \'seed\' AND project_id IS NULL'
    )
