"""Helpers to record `generation_calls` rows.

Provider clients return an `LLMResponse` (or analogous future dataclass);
the remake workers wrap the call with `record(...)` which writes the
ledger row and bumps the parent remake's `actual_cost_usd` roll-up when
a `remake_id` is supplied.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import update
from sqlmodel import Session

from app.core.metrics import cp_generation_call_latency_seconds, cp_generation_calls_total
from app.models.generation_calls import GenerationCall
from app.models.remake_shots import RemakeShot
from app.models.remakes import Remake


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
    remake_id: Optional[uuid.UUID] = None,
    remake_shot_id: Optional[uuid.UUID] = None,
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
    """Write one ledger row + roll up remake/shot cost when applicable."""
    row = GenerationCall(
        project_id=project_id,
        scenario_id=scenario_id,
        scene_idx=scene_idx,
        variant_id=variant_id,
        remake_id=remake_id,
        remake_shot_id=remake_shot_id,
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

    if cost_usd:
        # Numeric columns are Decimal in Python; adding a float breaks
        # SQLAlchemy's synchronize-by-evaluate path. Cast via str() to
        # avoid float-rounding artifacts.
        cost_decimal = cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))
        if remake_id is not None:
            session.exec(
                update(Remake)
                .where(Remake.id == remake_id)
                .values(actual_cost_usd=Remake.actual_cost_usd + cost_decimal)
                .execution_options(synchronize_session=False)
            )
        if remake_shot_id is not None:
            session.exec(
                update(RemakeShot)
                .where(RemakeShot.id == remake_shot_id)
                .values(actual_cost_usd=RemakeShot.actual_cost_usd + cost_decimal)
                .execution_options(synchronize_session=False)
            )

    session.flush()

    cp_generation_calls_total.labels(task_key=task_key, provider=provider, status=status_).inc()
    if latency_ms is not None:
        cp_generation_call_latency_seconds.labels(task_key=task_key, provider=provider).observe(latency_ms / 1000.0)

    return row
