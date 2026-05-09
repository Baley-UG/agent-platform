"""Template schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

TemplateKind = Literal["intro", "outro", "lower_third", "sticker", "transition"]


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: TemplateKind
    aspect_ratio: Optional[str] = None
    duration_sec: Optional[float] = None
    insertion_rules: Optional[dict] = None


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    kind: Optional[TemplateKind] = None
    aspect_ratio: Optional[str] = None
    duration_sec: Optional[float] = None
    insertion_rules: Optional[dict] = None
    video_s3_key: Optional[str] = None


class TemplateRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    kind: str
    video_s3_key: Optional[str]
    duration_sec: Optional[float]
    aspect_ratio: Optional[str]
    insertion_rules: Optional[dict]
    created_at: datetime

    model_config = {"from_attributes": True}
