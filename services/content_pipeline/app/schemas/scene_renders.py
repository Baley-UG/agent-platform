"""scene_renders schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class SceneRenderRead(BaseModel):
    id: uuid.UUID
    scenario_id: uuid.UUID
    scene_idx: int
    aspect_ratio: str
    image_asset_id: Optional[uuid.UUID]
    video_asset_id: Optional[uuid.UUID]
    # Phase 2 — director-resolved brand asset + LLM rationale +
    # img2img remix strength (None = legacy synth path).
    resolved_asset_id: Optional[uuid.UUID] = None
    match_reason: Optional[str] = None
    image_strength: Optional[float] = None
    # Phase 4 — reference frame seeded onto this cell at materialize
    # time. NULL means image_gen will fall through to pure t2i.
    init_image_s3_key: Optional[str] = None
    status: str
    error: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RegenerateImageRequest(BaseModel):
    """Body for `POST /scenarios/{id}/scenes/{idx}/regenerate-image`."""

    aspect_ratio: Optional[str] = None  # default: regenerate ALL aspect groups for this scene
    prompt_override: Optional[str] = None


class RegenerateVideoRequest(BaseModel):
    """Body for `POST /scenarios/{id}/scenes/{idx}/regenerate-video`."""

    aspect_ratio: Optional[str] = None  # default: regenerate ALL aspect groups for this scene
    motion_override: Optional[str] = None
