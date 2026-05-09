"""Music track schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

License = Literal["owned", "licensed", "public_domain"]


class MusicTrackCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    license: License = "owned"
    license_doc_url: Optional[str] = None
    bpm: Optional[int] = Field(default=None, ge=20, le=300)
    duration_sec: Optional[float] = Field(default=None, gt=0)
    mood: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class MusicTrackUpdate(BaseModel):
    name: Optional[str] = None
    license: Optional[License] = None
    license_doc_url: Optional[str] = None
    bpm: Optional[int] = Field(default=None, ge=20, le=300)
    duration_sec: Optional[float] = Field(default=None, gt=0)
    mood: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    audio_s3_key: Optional[str] = None


class MusicTrackRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    audio_s3_key: Optional[str]
    duration_sec: Optional[float]
    bpm: Optional[int]
    mood: Optional[List[str]]
    tags: Optional[List[str]]
    license: str
    license_doc_url: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
