"""Helpers to record `generation_calls` rows.

Provider clients return an `LLMResponse` (or analogous future dataclass);
the analyzer / image_gen / video_gen workers wrap the call with `record(...)`
which writes the ledger row and bumps the project's `scenarios.generation_cost_usd`
roll-up when a `scenario_id` is supplied.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import update
from sqlmodel import Session

from app.core.metrics import cp_generation_call_latency_seconds, cp_generation_calls_total
from app.models.generation_calls import GenerationCall
from app.models.scenarios import Scenario


def record(
    session: Session,
    *,
    project_id: uuid.UUID,
    task_key: str,
    provider: str,
    model_id: str,
    status_: str = "success",
    scenario_id: Optional[uuid.UUID] = None,
    scene_idx: Optional[int] = None,
    variant_id: Optional[uuid.UUID] = None,
    request_id: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    image_count: Optional[int] = None,
    video_seconds: Optional[float] = None,
    audio_seconds: Optional[float] = None,
    unit_count: Optional[int] = None,
    cost_usd: float = 0.0,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> GenerationCall:
    """Write one ledger row + roll up scenario cost when applicable."""
    row = GenerationCall(
        project_id=project_id,
        scenario_id=scenario_id,
        scene_idx=scene_idx,
        variant_id=variant_id,
        task_key=task_key,
        provider=provider,
        model_id=model_id,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        image_count=image_count,
        video_seconds=video_seconds,
        audio_seconds=audio_seconds,
        unit_count=unit_count,
        cost_usd=cost_usd,
        status=status_,
        latency_ms=latency_ms,
        error=error,
    )
    session.add(row)

    if scenario_id is not None and cost_usd:
        session.exec(
            update(Scenario)
            .where(Scenario.id == scenario_id)
            .values(generation_cost_usd=Scenario.generation_cost_usd + cost_usd)
        )

    session.flush()

    cp_generation_calls_total.labels(task_key=task_key, provider=provider, status=status_).inc()
    if latency_ms is not None:
        cp_generation_call_latency_seconds.labels(task_key=task_key, provider=provider).observe(latency_ms / 1000.0)

    return row
