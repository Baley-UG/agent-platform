"""Request/response shapes for `/admin/projects/...`."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


ProjectStatus = Literal["active", "archived", "paused"]
ReusePolicy = Literal["block", "warn", "silent"]


class ProjectCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    status: ProjectStatus = "active"
    reuse_policy: ReusePolicy = "warn"
    weekly_budget_cap_usd: Optional[Decimal] = Field(default=None, ge=0)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    status: Optional[ProjectStatus] = None
    reuse_policy: Optional[ReusePolicy] = None
    weekly_budget_cap_usd: Optional[Decimal] = Field(default=None, ge=0)
    default_brand_kit_id: Optional[UUID] = None
    default_social_account_id: Optional[UUID] = None


class ProjectRead(BaseModel):
    id: UUID
    slug: str
    name: str
    status: ProjectStatus
    reuse_policy: ReusePolicy
    weekly_budget_cap_usd: Optional[Decimal] = None
    default_brand_kit_id: Optional[UUID] = None
    default_social_account_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
