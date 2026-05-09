"""posting_strategy schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

FillStrategy = Literal["manual", "auto_suggest", "auto_fill"]
AutoGenerateMode = Literal["off", "suggest", "auto"]


class PostingStrategyUpdate(BaseModel):
    timezone: Optional[str] = Field(default=None, max_length=64)
    weekly_quota: Optional[dict] = None
    preferred_slots: Optional[dict] = None
    min_gap_minutes: Optional[dict] = None
    blackout: Optional[dict] = None
    fill_strategy: Optional[FillStrategy] = None
    auto_generate_if_empty: Optional[AutoGenerateMode] = None
    approval_required_before_publish: Optional[bool] = None
    weekly_budget_cap_usd: Optional[float] = None


class PostingStrategyRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    timezone: str
    weekly_quota: dict
    preferred_slots: dict
    min_gap_minutes: dict
    blackout: dict
    fill_strategy: str
    auto_generate_if_empty: str
    approval_required_before_publish: bool
    weekly_budget_cap_usd: Optional[float]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
