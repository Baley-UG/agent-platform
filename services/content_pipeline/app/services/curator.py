"""AI curator — LLM filter on top of `reference_intake_rules`.

For each candidate reference that passes the deterministic intake rules,
the curator asks the LLM "is this on-brand and reproducible?" and writes
a 0-1 score + a one-line reason to `content_references.curator_score` /
`curator_reason`.

References below `min_curator_score` (admin-set per project; default
0.5) get auto-archived. The score is also surfaced in the inbox UI so
admins can sort and skim.

CP-M8 ships the service + DB columns; the cron loop that runs the
curator across the inbox lands in CP-M8.5 once we have a representative
reference set to calibrate the prompt against.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from sqlmodel import Session

from app.core.logging import logger
from app.models.brand_kits import BrandKit
from app.models.content_references import ContentReference
from app.models.projects import Project
from app.services import generation_calls as calls_svc
from app.services import model_router
from app.services.providers.llm.openrouter import OpenRouterProvider


CURATOR_SYSTEM_PROMPT = (
    "You evaluate whether a reference video is a good candidate for a brand to "
    "reproduce. Score 0-1 (1 = great fit, 0 = unusable). Consider tone, originality "
    "potential, content type alignment, and whether AI generation is likely to "
    "reproduce the genre faithfully. Output STRICT JSON only:\n"
    '{"score": <float 0-1>, "reason": "<one short sentence>"}'
)


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _build_user_prompt(reference: ContentReference, brand_style_suffix: Optional[str]) -> str:
    parts = [
        f"Source: {reference.source_provider}",
        f"Caption: {(reference.caption or '').strip() or '(none)'}",
    ]
    meta = reference.metadata_json or {}
    for key in ("media_type", "duration_sec", "play_count", "like_count", "score"):
        if meta.get(key) is not None:
            parts.append(f"{key}: {meta[key]}")
    if reference.hashtags:
        parts.append("hashtags: " + " ".join(reference.hashtags))
    if brand_style_suffix:
        parts.append(f"\nBrand voice: {brand_style_suffix.strip()}")
    return "\n".join(parts)


def _parse_score(text: str) -> tuple[Optional[float], str]:
    cleaned = text.strip()
    if not cleaned.startswith("{"):
        m = _JSON_RE.search(cleaned)
        if m:
            cleaned = m.group(0)
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None, "parse_failed"
    score = payload.get("score")
    reason = (payload.get("reason") or "").strip()[:500]
    if isinstance(score, (int, float)):
        score = max(0.0, min(1.0, float(score)))
        return score, reason or "(no reason given)"
    return None, "missing_score"


async def curate(
    session: Session, project: Project, reference: ContentReference
) -> tuple[Optional[float], Optional[str]]:
    """Run the curator LLM on one reference. Writes the score to the row
    and returns (score, reason). Returns (None, None) when no LLM is
    configured (fail-open — admin can score manually)."""
    try:
        route = model_router.resolve(session, "scenario_analysis", project_id=project.id)
    except model_router.NoRouteError:
        return None, None

    # Pull brand voice for context.
    brand_style_suffix = None
    if project.default_brand_kit_id:
        kit = session.get(BrandKit, project.default_brand_kit_id)
        if kit and kit.style_prompt_suffix:
            brand_style_suffix = kit.style_prompt_suffix

    provider = OpenRouterProvider()
    user_prompt = _build_user_prompt(reference, brand_style_suffix)
    try:
        response = await provider.complete(
            prompt=user_prompt,
            route=route,
            system=CURATOR_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("curator_call_failed", reference_id=str(reference.id), error=str(exc))
        return None, None

    score, reason = _parse_score(response.text)
    if score is None:
        logger.warning("curator_parse_failed", reference_id=str(reference.id), preview=response.text[:200])

    reference.curator_score = score
    reference.curator_reason = reason
    session.add(reference)
    session.flush()

    calls_svc.record(
        session,
        project_id=project.id,
        task_key="curator",
        provider=route.provider,
        model_id=route.model_id,
        request_id=response.request_id,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        cost_usd=response.cost_usd,
        latency_ms=response.latency_ms,
        status_="success" if score is not None else "failed",
    )

    return score, reason
