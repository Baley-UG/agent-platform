"""reference_intake_rules schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

IntakeAction = Literal["auto_import", "queue_for_review"]


class IntakeRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    conditions: dict = Field(default_factory=dict)
    action: IntakeAction = "queue_for_review"
    priority: int = 0


class IntakeRuleUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    conditions: Optional[dict] = None
    action: Optional[IntakeAction] = None
    priority: Optional[int] = None


class IntakeRuleRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    enabled: bool
    conditions: dict
    action: str
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
