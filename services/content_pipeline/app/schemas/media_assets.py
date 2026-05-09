"""media_assets read shapes — preview URLs and version history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class MediaAssetRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    type: str
    s3_key: str
    mime_type: Optional[str]
    size_bytes: Optional[int]
    width: Optional[int]
    height: Optional[int]
    duration_sec: Optional[float]
    parent_scenario_id: Optional[uuid.UUID]
    parent_scene_idx: Optional[int]
    version: int
    previous_version_id: Optional[uuid.UUID]
    replaced_by_id: Optional[uuid.UUID]
    metadata_json: Optional[dict]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PresignedReadResponse(BaseModel):
    asset_id: uuid.UUID
    s3_key: str
    preview_url: str
    expires_in: int


class AssetHistoryResponse(BaseModel):
    """Full version chain for an asset (oldest → newest)."""

    asset_id: uuid.UUID
    versions: List[MediaAssetRead]
    current_version: int
