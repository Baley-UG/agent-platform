"""weekly_plans + plan_slots schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

PlanStatus = Literal["draft", "approved", "active", "archived"]
SlotStatus = Literal["empty", "filling", "ready", "scheduled", "publishing", "published", "failed", "skipped"]
ContentType = Literal["post", "story", "reel", "tiktok_video"]


class WeeklyPlanRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    week_start_date: date
    status: str
    generated_at: datetime
    generated_by: Optional[str]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WeeklyPlanGenerateRequest(BaseModel):
    week_start: date = Field(..., description="Any date inside the target week — Monday is computed.")
    fill: bool = True


class PlanSlotRead(BaseModel):
    id: uuid.UUID
    weekly_plan_id: uuid.UUID
    project_id: uuid.UUID
    scheduled_at: datetime
    social_account_id: Optional[uuid.UUID]
    content_type: str
    variant_preset: str
    source_kind: str
    variant_id: Optional[uuid.UUID]
    reference_id: Optional[uuid.UUID]
    status: str
    suggested_variant_ids: Optional[List[uuid.UUID]]
    publish_job_id: Optional[uuid.UUID]
    caption_override: Optional[str] = None
    hashtags_override: Optional[List[str]] = None
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PlanSlotCreate(BaseModel):
    weekly_plan_id: uuid.UUID
    scheduled_at: datetime
    social_account_id: Optional[uuid.UUID] = None
    content_type: ContentType
    variant_preset: str
    variant_id: Optional[uuid.UUID] = None


class PlanSlotUpdate(BaseModel):
    scheduled_at: Optional[datetime] = None
    social_account_id: Optional[uuid.UUID] = None
    variant_id: Optional[uuid.UUID] = None
    reference_id: Optional[uuid.UUID] = None
    status: Optional[SlotStatus] = None
    content_type: Optional[ContentType] = None
    variant_preset: Optional[str] = None
    caption_override: Optional[str] = None
    hashtags_override: Optional[List[str]] = None


class AssignVariantRequest(BaseModel):
    variant_id: uuid.UUID
