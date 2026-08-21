"""fal.ai remake steps — generic worker (no ffmpeg).

Phase 1 handlers: asr (whisper), erase (video inpaint). Phase 2 wires
restyle / keyframe_edit / i2v; they're registered here so an
accidentally-authored step fails loudly rather than silently hanging.

All fal calls go through the shared `FalQueueClient` with a real
fallback chain (resolve_chain) and a ledger row per attempt.
"""

from __future__ import annotations

from typing import List

from sqlmodel import Session

from app.core import s3 as s3lib
from app.core.logging import logger
from app.models.model_routes import ModelRoute
from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep
from app.models.remakes import Remake
from app.services import generation_calls as calls_svc
from app.services import model_router
from app.services.providers import fal_queue
from app.workers import remake_common as common


def _routes(session: Session, task_key: str, project_id) -> List[ModelRoute]:
    routes = model_router.resolve_chain(session, task_key, project_id=project_id)
    if not routes:
        raise RuntimeError(f"no model route for task_key={task_key}")
    return routes


def _presign(key: str) -> str:
    """A URL fal can fetch. Presigned against the public endpoint; works
    in prod (Hetzner). Local MinIO isn't reachable by fal, so AI steps
    only run end-to-end against a public bucket."""
    return s3lib.presigned_get_url(key, ttl=3600)


def _download_output(url: str, work: str, name: str) -> str:
    import httpx

    path = f"{work}/{name}"
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(resp.content)
    return path


def _asr(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """fal whisper over the source video → remake.asr_json.

    BEST-EFFORT: the transcript only sharpens the plan, it isn't required
    (many ads have no speech, and fal can't fetch a private/local bucket
    in dev). So this NEVER raises — a failure records an empty transcript
    and succeeds, which keeps `author_plan` unblocked. The reconciler's
    dependency check treats only succeeded/skipped as satisfied, so a
    hard failure here would otherwise stall the whole analysis phase.
    """
    try:
        routes = _routes(session, "remake_asr", remake.project_id)
        audio_url = _presign(remake.source_s3_key)

        def body_for(route: ModelRoute) -> dict:
            return {"audio_url": audio_url, "chunk_level": "word", **(route.params or {})}

        result = fal_queue.run_task(routes, body_for, timeout_seconds=900)
        data = result.result or {}
        remake.asr_json = {"text": data.get("text", ""), "chunks": data.get("chunks", [])}
        session.add(remake)
        session.flush()
        calls_svc.record(
            session, project_id=remake.project_id, task_key="remake_asr",
            provider="fal", model_id=result.model_id, remake_id=remake.id,
            audio_seconds=remake.source_duration_sec, latency_ms=result.latency_ms,
        )
        return {"chars": len(remake.asr_json["text"])}
    except Exception as exc:  # noqa: BLE001 — transcript is optional
        logger.warning("remake_asr_skipped", remake_id=str(remake.id), error=str(exc))
        remake.asr_json = {"text": "", "chunks": [], "error": str(exc)[:200]}
        session.add(remake)
        session.flush()
        return {"chars": 0, "skipped": True}


def _erase(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """Video inpaint: remove branding from the cut clip. Consumes the
    upstream `cut` output; produces a clip for `normalize` to conform."""
    shot = session.get(RemakeShot, step.shot_id) if step.shot_id else None
    if shot is None:
        raise RuntimeError("erase step has no shot")
    src_key = common.prev_output_key(session, step)
    if not src_key:
        raise RuntimeError("erase has no upstream cut clip")

    routes = _routes(session, "shot_erase", remake.project_id)
    video_url = _presign(src_key)
    prompt = (shot.prompt or "logo, watermark, text").strip()

    def body_for(route: ModelRoute) -> dict:
        # VOID takes {video_url, prompt}; Bria erase/prompt is the same
        # shape. Both remove the described object without a mask.
        return {"video_url": video_url, "prompt": prompt, **(route.params or {})}

    def on_fail(route: ModelRoute, exc: Exception) -> None:
        calls_svc.record(
            session, project_id=remake.project_id, task_key="shot_erase",
            provider="fal", model_id=route.model_id, remake_id=remake.id,
            remake_shot_id=shot.id, status_="failed", error=str(exc)[:500],
        )

    result = fal_queue.run_task(routes, body_for, timeout_seconds=1200, on_attempt_failed=on_fail)
    if not result.output_url:
        raise RuntimeError(f"erase returned no video url ({result.model_id})")

    work = common.tempdir()
    try:
        local = _download_output(result.output_url, work, "erased.mp4")
        with open(local, "rb") as fh:
            data = fh.read()
        out_key = s3lib.make_key(remake.project_id, "scenes", f"{remake.id}-erase-{shot.idx:02d}.mp4")
        s3lib.upload_bytes(out_key, data, content_type="video/mp4")
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    calls_svc.record(
        session, project_id=remake.project_id, task_key="shot_erase",
        provider="fal", model_id=result.model_id, remake_id=remake.id,
        remake_shot_id=shot.id, latency_ms=result.latency_ms,
        cost_usd=shot.est_cost_usd or 0.0,
    )
    return {"s3_key": out_key}


def _not_wired(kind: str):
    def _h(session: Session, step: RemakeStep, remake: Remake) -> dict:
        raise RuntimeError(f"{kind} is a Phase 2 technique and is not wired yet")
    return _h


_HANDLERS = {
    "asr": _asr,
    "erase": _erase,
    # Phase 2 — fail loudly if authored early.
    "restyle": _not_wired("restyle"),
    "keyframe_edit_start": _not_wired("keyframe_edit"),
    "keyframe_edit_end": _not_wired("keyframe_edit"),
    "i2v": _not_wired("i2v"),
    "tts": _not_wired("tts"),
    "lipsync": _not_wired("lipsync"),
    "upscale": _not_wired("upscale"),
}


def run(step_id: str) -> dict:
    return common.run_step(step_id, _HANDLERS)
