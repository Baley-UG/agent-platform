"""Cost reporting schemas — generation_calls aggregations."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class GenerationCallRead(BaseModel):
    id: uuid.UUID
    scenario_id: Optional[uuid.UUID]
    scene_idx: Optional[int]
    variant_id: Optional[uuid.UUID]
    task_key: str
    provider: str
    model_id: str
    request_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cached_tokens: Optional[int]
    image_count: Optional[int]
    video_seconds: Optional[float]
    audio_seconds: Optional[float]
    unit_count: Optional[int]
    cost_usd: float
    status: str
    latency_ms: Optional[int]
    error: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskBreakdown(BaseModel):
    task_key: str
    call_count: int
    success_count: int
    failed_count: int
    cost_usd: float


class CostSummary(BaseModel):
    """Project cost summary across an arbitrary time window."""

    project_id: uuid.UUID
    period_from: datetime
    period_to: datetime
    total_cost_usd: float
    total_calls: int
    success_calls: int
    failed_calls: int
    by_task: List[TaskBreakdown]
    by_provider: List[dict]
    weekly_budget_cap_usd: Optional[float]
    weekly_budget_remaining_usd: Optional[float]
