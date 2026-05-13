"""Analyzer RQ task.

Entry point: `app.workers.analyzer.run(scenario_id)` — dispatched from the
API when an admin creates or regenerates a scenario.

Resolves the LLM route → calls the provider → records the `generation_calls`
row → moves the scenario to `pending_review` (or `failed`).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.scenarios import Scenario
from app.services import generation_calls as calls_svc
from app.services import model_router
from app.services import scenarios as scenarios_svc
from app.services.analyzer import analyze_reference
from app.services.database import session_scope
from app.services.providers.llm.base import LLMProvider, VisionInput
from app.services.providers.llm.openrouter import OpenRouterProvider


# Max images sent to the vision LLM for one analysis. Each image costs
# ~700-1000 tokens on Claude/GPT-vision; 4 keeps cost bounded while
# covering most carousels (3-5 slides typical) without missing slides.
_MAX_VISION_IMAGES = 4


def _collect_vision_inputs(reference: ContentReference) -> list[VisionInput]:
    """Build the list of images to feed the vision LLM.

    Critical: presigned URLs to our private S3 (e.g. `http://minio:9000/...`)
    are NOT reachable from OpenRouter. We download bytes inside the
    worker and inline them as base64.

    Source preference:
      1. For carousels: pull EVERY slide's S3-mirrored bytes (up to
         `_MAX_VISION_IMAGES`). Without this we'd send just the cover
         slide and the LLM would fabricate the remaining scenes.
      2. For single photos / videos: just the poster (or media for an
         image post).
      3. Fallback to public IG CDN URLs if S3 mirror is missing — IG
         CDN IS publicly reachable.

    Returns `[]` when nothing usable; LLM falls back to text-only and
    we log a warning so it's obvious in operations.
    """
    import base64
    import io as _io

    from app.core import s3 as s3lib
    from app.core.config import settings

    inputs: list[VisionInput] = []
    meta = reference.metadata_json or {}
    media_type = meta.get("media_type")
    is_carousel = media_type == 8

    # ---- Pass 1: download whatever we have mirrored in S3 ----
    if s3lib.is_configured():
        keys: list[str] = []
        if is_carousel and reference.media_s3_key:
            # Today we only mirror the first slide. Find any further
            # slides under the same project prefix in metadata, then
            # mirror them on demand.
            keys.append(reference.media_s3_key)
        else:
            preferred = reference.poster_s3_key or reference.media_s3_key
            if preferred:
                keys.append(preferred)

        for k in keys[:_MAX_VISION_IMAGES]:
            try:
                buf = _io.BytesIO()
                s3lib.client().download_fileobj(settings.S3_BUCKET, k, buf)
                data = buf.getvalue()
                head = s3lib.head_object(k) or {}
                mime = head.get("ContentType") or _guess_mime_from_key(k)
                inputs.append(
                    VisionInput(
                        base64=base64.b64encode(data).decode("ascii"),
                        mime_type=mime,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("vision_s3_inline_failed", key=k, error=str(exc))

    # ---- Pass 2: pull missing carousel slides from IG CDN ----
    # For carousels, augment with the remaining IG media URLs from the
    # metadata so the LLM sees ALL slides, not just the cover. We send
    # these as direct URLs (IG CDN is publicly fetchable) so OpenRouter
    # can pull them. Falls back silently when the URL has expired.
    if is_carousel:
        ig_urls = meta.get("ig_media_urls") or []
        if isinstance(ig_urls, list):
            # Skip slide 0 (already covered by S3 mirror above), take next.
            remaining = ig_urls[1:_MAX_VISION_IMAGES]
            for url in remaining:
                if isinstance(url, str) and url:
                    inputs.append(VisionInput(url=url))

    # ---- Pass 3: total fallback to IG CDN if no S3 mirror at all ----
    if not inputs:
        cdn_url = meta.get("ig_thumbnail_url")
        if not cdn_url:
            ig_urls = meta.get("ig_media_urls") or []
            if isinstance(ig_urls, list) and ig_urls:
                cdn_url = ig_urls[0]
        if cdn_url:
            inputs.append(VisionInput(url=str(cdn_url)))

    return inputs[:_MAX_VISION_IMAGES]


def _guess_mime_from_key(key: str) -> str:
    """Best-effort mime guess from an S3 key extension."""
    lower = key.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def _build_provider(provider_name: str) -> LLMProvider:
    """Pick the concrete LLM provider matching a route's `provider` field.

    Add new providers here (anthropic_direct, openai_direct, ollama_local…)
    as they're implemented.
    """
    if provider_name == "openrouter":
        return OpenRouterProvider()
    raise NotImplementedError(f"LLM provider not yet implemented: {provider_name}")


def run(scenario_id: str, brand_style_suffix: Optional[str] = None) -> dict:
    """RQ entry point. Returns a dict summary so the job result page is useful."""
    scenario_uuid = uuid.UUID(scenario_id)

    with session_scope() as session:
        scenario = session.get(Scenario, scenario_uuid)
        if scenario is None:
            logger.warning("analyzer_scenario_missing", scenario_id=scenario_id)
            return {"ok": False, "error": "scenario not found"}

        try:
            if scenario.status == "draft":
                scenarios_svc.transition(scenario, "analyzing")
                session.add(scenario)
                session.flush()
        except scenarios_svc.InvalidStateTransition as exc:
            logger.warning("analyzer_bad_state", scenario_id=scenario_id, status=scenario.status, error=str(exc))
            return {"ok": False, "error": str(exc)}

        if scenario.reference_id is None:
            scenarios_svc.mark_failed(session, scenario, "scenario has no reference_id")
            return {"ok": False, "error": "no reference"}

        reference = session.get(ContentReference, scenario.reference_id)
        if reference is None:
            scenarios_svc.mark_failed(session, scenario, "reference not found")
            return {"ok": False, "error": "reference missing"}

        try:
            route = model_router.resolve(session, "scenario_analysis", project_id=scenario.project_id)
        except model_router.NoRouteError as exc:
            scenarios_svc.mark_failed(session, scenario, f"no LLM route: {exc}")
            return {"ok": False, "error": str(exc)}

        provider = _build_provider(route.provider)

        # Build vision_inputs from the reference's mirrored image (or
        # the still-live IG CDN URL as fallback) so the LLM actually
        # SEES what's in the reference instead of hallucinating from
        # caption text alone.
        vision_inputs = _collect_vision_inputs(reference)

        try:
            scenario_json, response = asyncio.run(
                analyze_reference(
                    reference=reference,
                    route=route,
                    provider=provider,
                    brand_style_suffix=brand_style_suffix,
                    vision_inputs=vision_inputs,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("analyzer_call_failed", scenario_id=scenario_id, error=str(exc))
            scenarios_svc.mark_failed(session, scenario, str(exc))
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                task_key="scenario_analysis",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            return {"ok": False, "error": str(exc)}

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            task_key="scenario_analysis",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            status_="success",
        )

        scenarios_svc.mark_pending_review(session, scenario, scenario_json)
        return {
            "ok": True,
            "scenario_id": str(scenario.id),
            "model": route.model_id,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_usd": response.cost_usd,
        }
