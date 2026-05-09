"""Project request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

ReusePolicy = Literal["block", "warn", "silent"]
ProjectStatus = Literal["active", "paused", "archived"]


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    reuse_policy: ReusePolicy = "warn"
    weekly_budget_cap_usd: Optional[float] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    status: Optional[ProjectStatus] = None
    reuse_policy: Optional[ReusePolicy] = None
    weekly_budget_cap_usd: Optional[float] = None
    default_brand_kit_id: Optional[uuid.UUID] = None
    default_social_account_id: Optional[uuid.UUID] = None


class ProjectRead(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    reuse_policy: str
    weekly_budget_cap_usd: Optional[float]
    default_brand_kit_id: Optional[uuid.UUID]
    default_social_account_id: Optional[uuid.UUID]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
