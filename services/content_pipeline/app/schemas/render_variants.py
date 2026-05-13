"""render_variants schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class RenderVariantRead(BaseModel):
    id: uuid.UUID
    scenario_id: uuid.UUID
    preset_key: str
    status: str
    final_asset_id: Optional[uuid.UUID]
    # Multi-asset variants (carousel posts) populate this list and leave
    # `final_asset_id` pointing at index 0 for legacy readers.
    final_asset_ids: Optional[List[uuid.UUID]] = None
    thumbnail_asset_id: Optional[uuid.UUID]
    duration_sec: Optional[float]
    file_size_bytes: Optional[int]
    error: Optional[str]
    created_at: datetime
    updated_at: datetime
    approved_at: Optional[datetime]

    model_config = {"from_attributes": True}


class RegenerateVoiceoverRequest(BaseModel):
    voice_id_override: Optional[str] = None
    text_override: Optional[str] = None


class ReselectMusicRequest(BaseModel):
    music_track_id: Optional[uuid.UUID] = None  # null → auto-pick from library
