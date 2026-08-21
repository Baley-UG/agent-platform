"""Pydantic schemas for the remake vertical."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class RemakeCreate(BaseModel):
    """Spawn a remake from a mirrored reference (ad_scraper or IG)."""

    reference_id: uuid.UUID
    brand_kit_id: Optional[uuid.UUID] = None
    # Output format. Omit → the service picks from the reference kind
    # (see presets.recommend_preset_for_reference).
    preset_key: Optional[str] = None


class ShotRead(BaseModel):
    id: uuid.UUID
    idx: int
    start_sec: float
    end_sec: float
    technique: str
    trim_start_sec: Optional[float] = None
    trim_end_sec: Optional[float] = None
    prompt: Optional[str] = None
    text_plan: Optional[list] = None
    frames: Optional[dict] = None
    tags: Optional[dict] = None
    status: str
    output_s3_key: Optional[str] = None
    est_cost_usd: Optional[float] = None
    actual_cost_usd: float = 0.0
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class StepRead(BaseModel):
    id: uuid.UUID
    shot_id: Optional[uuid.UUID] = None
    kind: str
    seq: int
    status: str
    attempts: int
    max_attempts: int
    est_cost_usd: Optional[float] = None
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class RemakeRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    reference_id: uuid.UUID
    brand_kit_id: Optional[uuid.UUID] = None
    preset_key: str
    status: str
    source_duration_sec: Optional[float] = None
    plan_json: Optional[dict] = None
    est_cost_usd: Optional[float] = None
    actual_cost_usd: float = 0.0
    final_s3_key: Optional[str] = None
    final_media_asset_id: Optional[uuid.UUID] = None
    default_caption: Optional[str] = None
    default_hashtags: Optional[List[str]] = None
    error: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RemakeDetail(RemakeRead):
    """Full read for the detail page: remake + shots + steps + progress."""

    shots: List[ShotRead] = []
    steps: List[StepRead] = []
    # {"shots_total": int, "shots_ready": int} — the x/y the panel shows.
    progress: dict = {}
    # Presigned URL of the composed video (final_review / done pages).
    final_url: Optional[str] = None


class ShotPatch(BaseModel):
    """One edited shot in the plan-review editor."""

    idx: int
    technique: Optional[Literal["copy", "erase", "restyle", "reframe", "drop"]] = None
    prompt: Optional[str] = None
    trim_start_sec: Optional[float] = Field(default=None, ge=0)
    trim_end_sec: Optional[float] = Field(default=None, ge=0)
    text_plan: Optional[list] = None


class PlanPatch(BaseModel):
    """PATCH body for `/remakes/{id}/plan` (plan_review only)."""

    shots: Optional[List[ShotPatch]] = None
    # Global plan_json fields.
    audio_mode: Optional[Literal["keep", "duck", "drop"]] = None
    voice_script: Optional[str] = None
    cta_text: Optional[str] = None
    outro_template_id: Optional[uuid.UUID] = None
    logo_overlay: Optional[dict] = None
    default_caption: Optional[str] = None
    default_hashtags: Optional[List[str]] = None


class ShotRejectRequest(BaseModel):
    """`/remakes/{id}/shots/{sid}/reject` — re-run one shot in final_review."""

    prompt_override: Optional[str] = None
    technique: Optional[Literal["copy", "erase", "restyle", "reframe", "drop"]] = None


class ApproveFinalRequest(BaseModel):
    plan_slot_id: Optional[uuid.UUID] = None
