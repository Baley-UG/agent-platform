"""LLM analysis remake steps — generic worker (no ffmpeg).

  tag_shots   — per-shot vision tagging (cheap model), fills shot.tags
  author_plan — one reasoning call that assigns each shot a technique +
                prompt + on-screen-text plan, and the global plan_json.

Both go through OpenRouter via the existing provider + model_router.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from typing import List, Optional

from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep
from app.models.remakes import Remake
from app.services import generation_calls as calls_svc
from app.services import model_router
from app.services import remake_cost as cost_svc
from app.services.providers.llm.base import VisionInput
from app.services.providers.llm.openrouter import OpenRouterProvider
from app.workers import remake_common as common

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
        cleaned = cleaned.rstrip("`").strip()
    if not cleaned.startswith("{"):
        m = _JSON_RE.search(cleaned)
        if not m:
            raise ValueError(f"no JSON object in LLM output: {text[:300]}")
        cleaned = m.group(0)
    return json.loads(cleaned)


def _inline_frame(key: str) -> Optional[VisionInput]:
    try:
        buf = io.BytesIO()
        s3lib.client().download_fileobj(settings.S3_BUCKET, key, buf)
        return VisionInput(base64=base64.b64encode(buf.getvalue()).decode("ascii"), mime_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        logger.warning("remake_frame_inline_failed", key=key, error=str(exc))
        return None


_TAG_SYSTEM = (
    "You audit one shot of a competitor video ad for remaking. Given up to 3 frames, "
    "return STRICT JSON only:\n"
    '{"description": "<one sentence: camera + subject + action>", '
    '"brand_visibility": {"logos": [{"name": "<or null>", "location": "<where>", "size": "small|medium|large"}], '
    '"products": ["..."], "onscreen_text": [{"text": "...", "location": "..."}]}, '
    '"faces": {"count": <int>, "talking_head": <bool>}, '
    '"motion": "static|slow|fast", "setting": "<short>"}\n'
    "Report ANY identifiable branding, watermark, or trademarked packaging. No prose, no fences."
)


def _tag_shots(session: Session, step: RemakeStep, remake: Remake) -> dict:
    shots = session.exec(
        select(RemakeShot).where(RemakeShot.remake_id == remake.id).order_by(RemakeShot.idx)
    ).all()
    if not shots:
        raise RuntimeError("tag_shots ran before scene_detect wrote shots")

    route = model_router.resolve(session, "remake_shot_tag", project_id=remake.project_id)
    provider = OpenRouterProvider()

    tagged = 0
    for shot in shots:
        frames = shot.frames or {}
        vis: List[VisionInput] = []
        for pos in ("start", "mid", "end"):
            if frames.get(pos):
                vi = _inline_frame(frames[pos])
                if vi:
                    vis.append(vi)
        if not vis:
            shot.tags = {"description": "", "brand_visibility": {}, "note": "no frames"}
            session.add(shot)
            continue
        try:
            resp = asyncio.run(
                provider.complete(
                    prompt=f"Audit shot #{shot.idx} ({float(shot.end_sec) - float(shot.start_sec):.1f}s).",
                    route=route, vision_inputs=vis, system=_TAG_SYSTEM,
                )
            )
            shot.tags = _parse_json(resp.text)
            calls_svc.record(
                session, project_id=remake.project_id, task_key="remake_shot_tag",
                provider=route.provider, model_id=route.model_id,
                remake_id=remake.id, remake_shot_id=shot.id,
                input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
                cost_usd=resp.cost_usd, latency_ms=resp.latency_ms,
            )
            tagged += 1
        except Exception as exc:  # noqa: BLE001 — a bad tag shouldn't fail the whole step
            logger.warning("remake_tag_shot_failed", shot=shot.idx, error=str(exc))
            shot.tags = {"description": "", "brand_visibility": {}, "error": str(exc)[:200]}
        session.add(shot)
    session.flush()
    return {"tagged": tagged, "shots": len(shots)}


_PLAN_SYSTEM = (
    "You author a shot-by-shot plan to remake a competitor's video ad as our own, near-identical copy.\n"
    "For EACH shot decide a technique, in this priority:\n"
    "1. NO visible branding → 'copy' (ship the real footage verbatim, free).\n"
    "2. small isolated logo/watermark → 'erase' (write a short inpaint prompt naming what to remove, e.g. 'logo top-right').\n"
    "3. prominent branding / branded packaging / too-recognizable talent → 'restyle' (write a video-to-video prompt keeping composition + camera motion + timing but swapping to OUR brand; reference @Image1=our logo).\n"
    "4. static product beauty shot → 'reframe'.\n"
    "5. pure competitor CTA card → 'drop'.\n"
    "For each shot also propose replacement on-screen text in our voice (or none).\n"
    "Globally decide keep vs re-voice audio (re-voice ONLY if the VO names the competitor).\n"
    "Return STRICT JSON only:\n"
    '{"shots": [{"idx": <int>, "technique": "copy|erase|restyle|reframe|drop", '
    '"reason": "<short>", "prompt": "<for erase/restyle/reframe, else null>", '
    '"on_screen_text": "<our line or empty>"}], '
    '"audio_mode": "keep|duck|drop", "voice_script": "<or null>", "cta_text": "<or null>"}\n'
    "No prose, no fences."
)


def _author_plan(session: Session, step: RemakeStep, remake: Remake) -> dict:
    shots = session.exec(
        select(RemakeShot).where(RemakeShot.remake_id == remake.id).order_by(RemakeShot.idx)
    ).all()
    if not shots:
        raise RuntimeError("author_plan ran before scene_detect wrote shots")

    reference = session.get(ContentReference, remake.reference_id)
    ref_meta = (reference.metadata_json or {}) if reference else {}

    # Build the context prompt.
    lines = ["# SHOTS"]
    for shot in shots:
        tag = shot.tags or {}
        bv = tag.get("brand_visibility") or {}
        lines.append(
            f"SHOT {shot.idx} ({float(shot.end_sec) - float(shot.start_sec):.1f}s): "
            f"{tag.get('description', '')} | logos={bv.get('logos') or 'none'} | "
            f"onscreen_text={bv.get('onscreen_text') or 'none'} | "
            f"faces={ (tag.get('faces') or {}).get('count', 0) }"
        )
    if reference and reference.caption:
        lines.append(f"\n# SOURCE CAPTION\n{reference.caption.strip()[:500]}")
    if remake.asr_json and remake.asr_json.get("text"):
        lines.append(f"\n# TRANSCRIPT\n{str(remake.asr_json['text'])[:1000]}")
    brand = ref_meta.get("advertisers") or []
    if brand:
        lines.append(f"\n# COMPETITOR\n{brand}")
    lines.append("\nAuthor the plan JSON now.")

    route = model_router.resolve(session, "remake_plan", project_id=remake.project_id)
    provider = OpenRouterProvider()
    resp = asyncio.run(
        provider.complete(prompt="\n".join(lines), route=route, system=_PLAN_SYSTEM)
    )
    plan = _parse_json(resp.text)
    calls_svc.record(
        session, project_id=remake.project_id, task_key="remake_plan",
        provider=route.provider, model_id=route.model_id, remake_id=remake.id,
        input_tokens=resp.input_tokens, output_tokens=resp.output_tokens,
        cost_usd=resp.cost_usd, latency_ms=resp.latency_ms,
    )

    # Fold decisions onto the shots.
    by_idx = {s.idx: s for s in shots}
    valid = {"copy", "erase", "restyle", "reframe", "drop"}
    for sp in plan.get("shots") or []:
        try:
            shot = by_idx.get(int(sp["idx"]))
        except (KeyError, TypeError, ValueError):
            continue
        if shot is None:
            continue
        tech = sp.get("technique")
        shot.technique = tech if tech in valid else "copy"
        shot.prompt = sp.get("prompt")
        ost = (sp.get("on_screen_text") or "").strip()
        shot.text_plan = [{"replacement": ost}] if ost else None
        session.add(shot)

    remake.plan_json = {
        "audio_mode": plan.get("audio_mode", "keep"),
        "voice_script": plan.get("voice_script"),
        "cta_text": plan.get("cta_text"),
        "brand_findings": [
            {"idx": s.idx, "logos": (s.tags or {}).get("brand_visibility", {}).get("logos")}
            for s in shots
            if (s.tags or {}).get("brand_visibility", {}).get("logos")
        ],
    }
    session.add(remake)
    session.flush()

    # Now that techniques are set, estimate cost for Gate 1.
    cost_svc.estimate_remake(session, remake)
    return {"shots_planned": len(shots)}


_HANDLERS = {
    "tag_shots": _tag_shots,
    "author_plan": _author_plan,
}


def run(step_id: str) -> dict:
    return common.run_step(step_id, _HANDLERS)
