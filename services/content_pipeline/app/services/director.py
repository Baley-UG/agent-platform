"""Director LLM — picks brand assets to fill each scene of a scenario.

Phase 2 (Brand-build mode). The director is a SINGLE multi-modal call:
  inputs:  reference frames + the brand asset library (thumb + tags)
  output:  storyboard mapping each scene → exact brand_asset.id (+ a
           short match_reason for admin transparency, + a strength hint
           for future img2img remix path)

Why single-call instead of per-scene loop:
- Cross-scene consistency — director sees the whole reference and the
  whole library at once, picks a COHERENT set rather than 4 independent
  best-matches that may clash stylistically.
- Cheaper — one prompt setup, not N. Vision token cost dominates; an
  extra structured-output completion is cheap.
- Smarter gap analysis — if 3 of 4 scenes need similar assets and the
  library only has 2 matching, the director can split them sensibly
  rather than reuse one asset 3 times.

LLM-as-matcher (not embeddings) for Phase 2 v1:
- Brand libraries are typically small (<50 assets). LLM can see them all.
- No pgvector infra needed; ships now.
- When libraries grow >100 assets, swap in CLIP+pgvector pre-filter
  (Phase 2.5) that hands the LLM the top-20 candidates.
"""

from __future__ import annotations

import base64
import io as _io
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.models.scenarios import Scenario
from app.services import model_router
from app.services.providers.llm.base import LLMProvider, VisionInput
from app.services.providers.llm.openrouter import OpenRouterProvider


# Hard cap on how many brand assets we send to the LLM. Vision token cost
# is proportional to image count; ~20 keeps a single call under $0.10
# typical. When the library exceeds this, we apply a cheap server-side
# pre-filter (by `brand_asset_type` mentioned in the prompt requirements).
_MAX_LIBRARY_ASSETS_SENT = 20
# Vision LLMs cap input images. We base64-inline so MinIO works in dev.
_MAX_REFERENCE_FRAMES = 4


@dataclass
class SceneAssignment:
    """One row of the director's output, per scene_idx."""

    scene_idx: int
    resolved_asset_id: Optional[uuid.UUID]
    match_reason: str
    confidence: float = 0.0
    image_strength: float = 0.6  # img2img hint for future Phase 2.5 remix path


@dataclass
class DirectorResult:
    """Aggregate output for one director run."""

    assignments: List[SceneAssignment]
    gaps: List[int]  # scene_idx with no matched asset (admin must fill)
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    raw_response: Optional[str] = None


SYSTEM_PROMPT = """You are a creative director for a brand's content pipeline.

You will receive:
1. The REFERENCE frames (1-4 images) of the inspiration content.
2. A SCENARIO JSON with `scenes[]` already broken down by the analyzer.
3. The brand's ASSET LIBRARY — a numbered list of available pre-shot
   images. Each asset has: index, type, mood, subjects, has_face.

Your job: pick the BEST asset for each scene_idx, OR mark it as a gap
when nothing in the library is a usable fit.

Rules:
- One asset per scene; the same asset CAN be reused if the scenes
  legitimately call for the same content (rare).
- Prefer continuity within the scenario — if scenes 1-4 are all
  "hero shot variations", pick assets that look like they belong to
  the same shoot (similar mood, similar palette).
- NEVER hallucinate an asset index that isn't in the library.
- NEVER pick a `face_*` asset for a non-face scene or vice versa.
- If the scene needs a person and no `face_*` asset matches, return
  resolved_asset_id=null (gap) and explain in match_reason what's
  missing — admin will shoot it.
- match_reason is ONE short sentence in the brand's working language
  (you should infer it from the analyzer's voiceover style).

Return STRICT JSON only:
{
  "assignments": [
    {
      "scene_idx": 1,
      "resolved_asset_index": 7,   // 0-based index INTO THE LIBRARY, or null
      "match_reason": "warm-lit founder portrait, matches the hook intent",
      "confidence": 0.85,
      "image_strength": 0.5        // 0..1; how much to allow img2img remix
                                    // 0.0 = use as-is, 1.0 = full reshoot
    },
    ...
  ]
}

No markdown, no fences, no prose outside the JSON."""


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _JSON_FENCE_RE.sub("", text).strip()


def _parse_director_response(text: str) -> dict:
    cleaned = _strip_fences(text).strip()
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]
    last = cleaned.rfind("}")
    if last >= 0:
        cleaned = cleaned[: last + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"director returned non-JSON: {cleaned[:200]}") from exc


def _reference_vision_inputs(reference: ContentReference) -> list[VisionInput]:
    """Pull up to _MAX_REFERENCE_FRAMES from the reference. Mirror of
    the analyzer logic — keep both in sync if you change one."""
    import base64

    inputs: list[VisionInput] = []
    meta = reference.metadata_json or {}
    is_carousel = meta.get("media_type") == 8

    if not s3lib.is_configured():
        return inputs

    # S3-mirrored slides first.
    keys: list[str] = []
    if is_carousel and reference.media_s3_key:
        keys.append(reference.media_s3_key)
    else:
        preferred = reference.poster_s3_key or reference.media_s3_key
        if preferred:
            keys.append(preferred)

    for k in keys[:_MAX_REFERENCE_FRAMES]:
        try:
            buf = _io.BytesIO()
            s3lib.client().download_fileobj(settings.S3_BUCKET, k, buf)
            data = buf.getvalue()
            head = s3lib.head_object(k) or {}
            mime = head.get("ContentType") or "image/jpeg"
            inputs.append(
                VisionInput(
                    base64=base64.b64encode(data).decode("ascii"),
                    mime_type=mime,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "director_reference_inline_failed", key=k, error=str(exc)
            )

    # Carousel slides 1..N from IG CDN (already public).
    if is_carousel and len(inputs) < _MAX_REFERENCE_FRAMES:
        ig_urls = meta.get("ig_media_urls") or []
        if isinstance(ig_urls, list):
            for url in ig_urls[1 : _MAX_REFERENCE_FRAMES]:
                if isinstance(url, str) and url:
                    inputs.append(VisionInput(url=url))

    return inputs[:_MAX_REFERENCE_FRAMES]


def _asset_vision_input(asset: MediaAsset) -> Optional[VisionInput]:
    """Download a brand asset's bytes and base64-inline for the LLM."""
    if not s3lib.is_configured() or not asset.s3_key:
        return None
    mime = (asset.mime_type or "").lower()
    if not mime.startswith("image/"):
        # Phase 2 v1 — videos are referenced by metadata only. Phase 3
        # adds keyframe extraction so the director can SEE video assets.
        return None
    try:
        buf = _io.BytesIO()
        s3lib.client().download_fileobj(settings.S3_BUCKET, asset.s3_key, buf)
        return VisionInput(
            base64=base64.b64encode(buf.getvalue()).decode("ascii"),
            mime_type=mime,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "director_asset_inline_failed",
            asset_id=str(asset.id),
            error=str(exc),
        )
        return None


def _select_library_assets(
    session: Session, project_id: uuid.UUID, brand_kit_id: Optional[uuid.UUID]
) -> list[MediaAsset]:
    """Pull the candidate pool for this scenario.

    Scoped to the project. If the scenario's brand_kit is set, prefer
    that kit's assets; otherwise include all kits for the project.
    `replaced_by_id IS NULL` ensures we don't pick stale versions.
    """
    stmt = (
        select(MediaAsset)
        .where(
            MediaAsset.project_id == project_id,
            MediaAsset.type == "brand_library",
            MediaAsset.status == "ready",
            MediaAsset.replaced_by_id.is_(None),
            MediaAsset.brand_asset_type.is_not(None),
        )
        .order_by(MediaAsset.created_at.desc())
        .limit(_MAX_LIBRARY_ASSETS_SENT * 2)  # over-fetch, kit-filter below
    )
    rows = list(session.exec(stmt).all())

    if brand_kit_id is not None:
        # Prefer kit-matched assets; pad with un-kitted ones if room.
        kit_matched = [r for r in rows if r.brand_kit_id == brand_kit_id]
        others = [r for r in rows if r.brand_kit_id != brand_kit_id]
        rows = (kit_matched + others)[:_MAX_LIBRARY_ASSETS_SENT]
    else:
        rows = rows[:_MAX_LIBRARY_ASSETS_SENT]
    return rows


def _build_library_text_block(assets: list[MediaAsset]) -> str:
    """Format the asset library as a numbered text block alongside the
    thumbnails. The LLM correlates numbers to images in the attached
    order; the text block carries the tag metadata it can't infer
    just from looking."""
    lines: list[str] = []
    for idx, a in enumerate(assets):
        tags = a.brand_asset_tags or {}
        mood = tags.get("mood") or "—"
        subjects = ",".join((tags.get("subjects") or [])[:4]) or "—"
        has_face = "yes" if tags.get("has_face") else "no"
        lines.append(
            f"[{idx}] type={a.brand_asset_type or '?'} "
            f"mood={mood} face={has_face} subjects={subjects}"
        )
    return "\n".join(lines) if lines else "(no assets in library)"


def _build_user_prompt(
    scenario: Scenario, reference: ContentReference, library_text: str
) -> str:
    """Compose the user-role text the director reads alongside the
    image attachments."""
    scenario_json = scenario.scenario_json or {}
    scenes = scenario_json.get("scenes") or []
    scenes_block = json.dumps(
        [
            {
                "idx": s.get("idx"),
                "duration": s.get("duration"),
                "shot_type": s.get("shot_type"),
                "voiceover": s.get("voiceover"),
                "on_screen_text": s.get("on_screen_text"),
                "image_prompt": s.get("image_prompt"),
            }
            for s in scenes
            if isinstance(s, dict)
        ],
        ensure_ascii=False,
        indent=2,
    )
    return (
        "## REFERENCE METADATA\n"
        f"caption: {reference.caption or '(none)'}\n"
        f"transcript: {reference.transcript or '(none)'}\n"
        f"hashtags: {', '.join(reference.hashtags or []) or '(none)'}\n\n"
        "## SCENARIO SCENES (analyzer output)\n"
        f"{scenes_block}\n\n"
        "## ASSET LIBRARY (matched by index in the attached thumbnails)\n"
        f"{library_text}\n\n"
        "Pick one asset per scene_idx. Return strict JSON per the schema."
    )


def _coerce_assignment(
    raw: dict, library: list[MediaAsset], scene_indices: list[int]
) -> SceneAssignment:
    """One LLM-output row → typed SceneAssignment. Out-of-range or
    malformed indices map to a gap; we never raise."""
    try:
        scene_idx = int(raw.get("scene_idx"))
    except (TypeError, ValueError):
        scene_idx = scene_indices[0] if scene_indices else 0

    raw_idx = raw.get("resolved_asset_index")
    asset_id: Optional[uuid.UUID] = None
    if isinstance(raw_idx, int) and 0 <= raw_idx < len(library):
        asset_id = library[raw_idx].id

    match_reason = str(raw.get("match_reason") or "")[:500]
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        strength = max(0.0, min(1.0, float(raw.get("image_strength") or 0.6)))
    except (TypeError, ValueError):
        strength = 0.6

    return SceneAssignment(
        scene_idx=scene_idx,
        resolved_asset_id=asset_id,
        match_reason=match_reason,
        confidence=confidence,
        image_strength=strength,
    )


def _build_provider(provider_name: str) -> LLMProvider:
    if provider_name == "openrouter":
        return OpenRouterProvider()
    raise NotImplementedError(f"LLM provider not implemented: {provider_name}")


async def run_director(
    *,
    session: Session,
    project: Project,
    scenario: Scenario,
    reference: ContentReference,
) -> DirectorResult:
    """Pure-async director call. Caller persists the result to
    scene_renders + scenarios via `apply_director_result`."""
    scenario_json = scenario.scenario_json or {}
    scenes = scenario_json.get("scenes") or []
    scene_indices = [s.get("idx") for s in scenes if isinstance(s, dict) and "idx" in s]

    library = _select_library_assets(
        session, project.id, brand_kit_id=getattr(scenario, "brand_kit_id", None)
    )
    if not library:
        logger.info(
            "director_no_library_assets", scenario_id=str(scenario.id)
        )
        return DirectorResult(
            assignments=[],
            gaps=list(scene_indices),
        )

    # Build vision inputs: reference frames FIRST, then library thumbs.
    # Order matters — the system prompt instructs the LLM to correlate
    # `[N]` indices in the text block with the attached images in this
    # exact order (reference first is metadata, library indices start
    # AFTER the reference frames in attachment order, but the LLM
    # learns this from "matched by index in the attached thumbnails").
    ref_inputs = _reference_vision_inputs(reference)
    asset_inputs: list[VisionInput] = []
    kept_assets: list[MediaAsset] = []
    for a in library:
        v = _asset_vision_input(a)
        if v is not None:
            asset_inputs.append(v)
            kept_assets.append(a)
    if not asset_inputs:
        return DirectorResult(
            assignments=[], gaps=list(scene_indices)
        )

    library_text = _build_library_text_block(kept_assets)
    user_prompt = _build_user_prompt(scenario, reference, library_text)

    route = model_router.resolve(session, "scenario_analysis", project.id)
    if route is None:
        logger.warning("director_no_route", project_id=str(project.id))
        return DirectorResult(
            assignments=[],
            gaps=list(scene_indices),
        )
    provider = _build_provider(route.provider)

    started = time.monotonic()
    response = await provider.complete(
        prompt=user_prompt,
        route=route,
        vision_inputs=ref_inputs + asset_inputs,
        system=SYSTEM_PROMPT,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        raw = _parse_director_response(response.text)
    except ValueError as exc:
        logger.warning(
            "director_parse_failed",
            scenario_id=str(scenario.id),
            error=str(exc),
        )
        return DirectorResult(
            assignments=[],
            gaps=list(scene_indices),
            cost_usd=response.cost_usd,
            latency_ms=latency_ms,
            raw_response=response.text[:2000],
        )

    raw_assignments = raw.get("assignments") or []
    if not isinstance(raw_assignments, list):
        raw_assignments = []

    assignments: list[SceneAssignment] = []
    for row in raw_assignments:
        if not isinstance(row, dict):
            continue
        assignments.append(_coerce_assignment(row, kept_assets, scene_indices))

    # Anything the director omitted is a gap.
    seen = {a.scene_idx for a in assignments}
    gaps = [idx for idx in scene_indices if idx not in seen]
    # …plus any explicit nulls.
    gaps += [a.scene_idx for a in assignments if a.resolved_asset_id is None]

    logger.info(
        "director_run",
        scenario_id=str(scenario.id),
        library_size=len(kept_assets),
        assignments=len(assignments),
        gaps=len(gaps),
        cost_usd=response.cost_usd,
    )
    return DirectorResult(
        assignments=assignments,
        gaps=sorted(set(gaps)),
        cost_usd=response.cost_usd,
        latency_ms=latency_ms,
        raw_response=response.text[:2000],
    )


def apply_director_result(
    *,
    session: Session,
    scenario: Scenario,
    result: DirectorResult,
) -> int:
    """Persist director output to `scene_renders`.

    Returns the number of rows that got a resolved_asset_id stamped.
    NB: scene_renders for this scenario must already be materialized
    (the caller is `scenarios_svc.start_image_generation` or the new
    `run-director` endpoint, both of which materialize beforehand).
    """
    from app.models.scene_renders import SceneRender

    updates = 0
    for assignment in result.assignments:
        if assignment.resolved_asset_id is None:
            continue
        rows = session.exec(
            select(SceneRender).where(
                SceneRender.scenario_id == scenario.id,
                SceneRender.scene_idx == assignment.scene_idx,
            )
        ).all()
        for row in rows:
            row.resolved_asset_id = assignment.resolved_asset_id
            row.match_reason = assignment.match_reason
            # Phase 2.5 — persist the LLM's remix-strength hint. The
            # image_gen worker reads this to decide between pure
            # passthrough (low strength) and img2img (higher strength).
            row.image_strength = assignment.image_strength
            session.add(row)
            updates += 1
    session.flush()
    return updates
