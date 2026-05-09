"""CP-M4 — point the global `scene_video` route at fal.ai's Seedance queue endpoint.

The CP-M2 seed used the ByteDance shorthand `seedance-v1-pro-i2v`, which only
made sense if we wired a direct Volcano Engine client. We're going through
fal.ai (consistent with `scene_image`), so the model_id needs to match
fal.ai's actual route name.

Revision ID: 0005_cp_m4
Revises: 0004_cp_m3
Create Date: 2026-05-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_cp_m4"
down_revision: Union[str, Sequence[str], None] = "0004_cp_m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "content_pipeline"

NEW_MODEL_ID = "fal-ai/bytedance/seedance/v1/pro/image-to-video"
OLD_MODEL_ID = "seedance-v1-pro-i2v"


def upgrade() -> None:
    op.execute(
        sa.text(
            f'UPDATE "{SCHEMA}".model_routes '
            f"SET model_id = :new, updated_at = now() "
            f"WHERE project_id IS NULL "
            f"  AND task_key = 'scene_video' "
            f"  AND provider = 'seedance' "
            f"  AND model_id = :old"
        ).bindparams(new=NEW_MODEL_ID, old=OLD_MODEL_ID)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            f'UPDATE "{SCHEMA}".model_routes '
            f"SET model_id = :old, updated_at = now() "
            f"WHERE project_id IS NULL "
            f"  AND task_key = 'scene_video' "
            f"  AND provider = 'seedance' "
            f"  AND model_id = :new"
        ).bindparams(new=NEW_MODEL_ID, old=OLD_MODEL_ID)
    )
