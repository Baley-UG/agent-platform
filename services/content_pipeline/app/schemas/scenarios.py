"""scenarios schemas — create/edit/approve/regenerate."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

QualityTier = Literal["draft", "final"]


class ScenarioCreate(BaseModel):
    """Spawn a scenario from a reference. The analyzer worker fills scenario_json."""

    reference_id: uuid.UUID
    target_variants: List[str] = Field(default_factory=lambda: ["ig_reels"])
    quality_tier: QualityTier = "final"
    # Reuse policy bypass — required when the reference has been used before
    # in projects with reuse_policy='warn'. Ignored when policy='block' (always denied)
    # or 'silent' (never enforced).
    force: bool = False
    reuse_reason: str = ""
    notes: Optional[str] = None


class ScenarioUpdate(BaseModel):
    """Admin edit of the scenario JSON before approval."""

    scenario_json: Optional[dict] = None
    target_variants: Optional[List[str]] = None
    quality_tier: Optional[QualityTier] = None
    notes: Optional[str] = None
    default_caption: Optional[str] = None
    default_hashtags: Optional[List[str]] = None


class ScenarioRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    reference_id: Optional[uuid.UUID]
    status: str
    scenario_json: Optional[dict]
    version: int
    target_variants: Optional[List[str]]
    target_aspect_groups: Optional[List[str]]
    quality_tier: str
    generation_cost_usd: float
    default_caption: Optional[str] = None
    default_hashtags: Optional[List[str]] = None
    voiceover_asset_id: Optional[uuid.UUID] = None
    music_track_id: Optional[uuid.UUID] = None
    last_error: Optional[str]
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReuseConflict(BaseModel):
    """Returned with HTTP 409 when reuse_policy blocks scenario creation."""

    error: Literal["reference_already_used"] = "reference_already_used"
    previously_used: bool
    usage_count: int
    last_used_days_ago: Optional[int]
    previous_scenarios: List[dict]
    project_reuse_policy: str
    hint: str = "set force=true (when reuse_policy='warn') and supply reuse_reason to override"
