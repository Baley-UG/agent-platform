"""Brand kit schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class BrandKitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    is_default: bool = False
    font_family: Optional[str] = None
    primary_color: Optional[str] = Field(default=None, pattern=r"^#?[0-9A-Fa-f]{3,8}$")
    secondary_color: Optional[str] = Field(default=None, pattern=r"^#?[0-9A-Fa-f]{3,8}$")
    voice_id: Optional[str] = None
    tts_lang: Optional[str] = None
    tts_settings: Optional[dict] = None
    style_prompt_suffix: Optional[str] = None


class BrandKitUpdate(BaseModel):
    name: Optional[str] = None
    is_default: Optional[bool] = None
    logo_s3_key: Optional[str] = None
    font_family: Optional[str] = None
    primary_color: Optional[str] = Field(default=None, pattern=r"^#?[0-9A-Fa-f]{3,8}$")
    secondary_color: Optional[str] = Field(default=None, pattern=r"^#?[0-9A-Fa-f]{3,8}$")
    voice_id: Optional[str] = None
    tts_lang: Optional[str] = None
    tts_settings: Optional[dict] = None
    style_prompt_suffix: Optional[str] = None


class BrandKitRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    is_default: bool
    logo_s3_key: Optional[str]
    font_family: Optional[str]
    primary_color: Optional[str]
    secondary_color: Optional[str]
    voice_id: Optional[str]
    tts_lang: Optional[str]
    tts_settings: Optional[dict]
    style_prompt_suffix: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
