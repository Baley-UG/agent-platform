"""auto_generation_rules schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

PickStrategy = Literal["highest_score", "newest", "diverse"]
QualityTier = Literal["draft", "final"]


class AutoGenRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    pick_strategy: PickStrategy = "highest_score"
    daily_quota: int = Field(default=1, ge=1, le=100)
    target_variants: Optional[List[str]] = None
    quality_tier: QualityTier = "final"
    budget_cap_usd: Optional[float] = Field(default=None, ge=0)


class AutoGenRuleUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    pick_strategy: Optional[PickStrategy] = None
    daily_quota: Optional[int] = Field(default=None, ge=1, le=100)
    target_variants: Optional[List[str]] = None
    quality_tier: Optional[QualityTier] = None
    budget_cap_usd: Optional[float] = Field(default=None, ge=0)


class AutoGenRuleRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    enabled: bool
    pick_strategy: str
    daily_quota: int
    target_variants: Optional[List[str]]
    quality_tier: str
    budget_cap_usd: Optional[float]
    last_run_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AutoGenRuleRunResponse(BaseModel):
    """Returned from POST /run-now — what got spawned (or why nothing did)."""

    rule_id: uuid.UUID
    spawned_scenario_id: Optional[uuid.UUID]
    reason: Optional[str] = None
