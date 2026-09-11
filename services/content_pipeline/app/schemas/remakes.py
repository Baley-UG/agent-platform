"""Pydantic schemas for the remake vertical."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class RemakeCreate(BaseModel):
    """Spawn a remake from a mirrored reference (ad_scraper or IG)."""

    reference_id: uuid.UUID
    brand_kit_id: Optional[uuid.UUID] = None
    # Output format. Omit → the service picks from the reference kind
    # (see presets.recommend_preset_for_reference).
    preset_key: Optional[str] = None


class RemakeImportExternal(BaseModel):
    """Register an externally-produced video against a reference.

    Lands at Gate 2 (`final_review`) so the human approve → done →
    stock/publish flow applies unchanged. Exactly ONE of:
      - `video_url` — fetchable by this service (public URL or presigned
        S3 GET); the file is streamed in.
      - `s3_key` — an object already in our bucket, e.g. uploaded via
        the presigned-PUT flow (`POST /assets/upload-url`); server-side
        copied into the finals prefix, no bytes move through the API.
    """

    reference_id: uuid.UUID
    video_url: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    s3_key: Optional[str] = Field(default=None, min_length=3, max_length=512)
    caption: Optional[str] = None
    # What the external production actually cost (USD) — lands in
    # `actual_cost_usd` so manual uploads show up in cost reporting too.
    cost_usd: Optional[float] = Field(default=None, ge=0, le=100_000)

    @model_validator(mode="after")
    def _exactly_one_source(self):
        if bool(self.video_url) == bool(self.s3_key):
            raise ValueError("pass exactly one of video_url or s3_key")
        return self


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
    # Enriched by the API from the source reference: a presigned poster
    # thumb for list cards and the reference's human name.
    poster_url: Optional[str] = None
    reference_title: Optional[str] = None

    model_config = {"from_attributes": True}


class RemakeDetail(RemakeRead):
    """Full read for the detail page: remake + shots + steps + progress."""

    shots: List[ShotRead] = []
    steps: List[StepRead] = []
    # {"shots_total": int, "shots_ready": int} — the x/y the panel shows.
    progress: dict = {}
    # Presigned URL of the composed video (final_review / done pages).
    final_url: Optional[str] = None
    # Presigned URL of the SOURCE reference video — the review page shows
    # it side by side with the remake.
    source_url: Optional[str] = None
    # Server-generated per-second frame strips for the timeline:
    # {"source": [urls], "final": [urls]}. None until the ffmpeg worker
    # has produced them (the panel falls back to client-side capture).
    filmstrip: Optional[dict] = None


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


class RejectFinalRequest(BaseModel):
    """Optional human note on WHY the final was rejected — stored on
    the remake and shown on its detail page."""

    reason: Optional[str] = Field(default=None, max_length=500)
