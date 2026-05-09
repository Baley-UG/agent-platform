"""Model route schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

CostUnit = Literal["input_token", "output_token", "image", "video_second", "audio_second", "call"]


class ModelRouteCreate(BaseModel):
    task_key: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=255)
    params: Optional[dict] = None
    priority: int = 0
    enabled: bool = True
    cost_unit: Optional[CostUnit] = None
    cost_per_unit_usd: Optional[float] = None


class ModelRouteUpdate(BaseModel):
    provider: Optional[str] = None
    model_id: Optional[str] = None
    params: Optional[dict] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    cost_unit: Optional[CostUnit] = None
    cost_per_unit_usd: Optional[float] = None


class ModelRouteRead(BaseModel):
    id: uuid.UUID
    project_id: Optional[uuid.UUID]
    task_key: str
    provider: str
    model_id: str
    params: Optional[dict]
    priority: int
    enabled: bool
    cost_unit: Optional[str]
    cost_per_unit_usd: Optional[float]
    pricing_updated_at: Optional[datetime]
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
