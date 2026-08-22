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


# ---------------------------------------------------------------------------
# Phase 2 — generative techniques
# ---------------------------------------------------------------------------


def _brand_ref_urls(session: Session, remake: Remake, limit: int = 6) -> list[str]:
    """Presigned URLs of the brand's reference images for the generative
    models: the logo first, then any brand-library assets. These are the
    @Image refs Kling / nano-banana composite into the reshot frame."""
    from app.models.brand_kits import BrandKit
    from app.models.media_assets import MediaAsset
    from sqlmodel import select as _select

    urls: list[str] = []
    kit = None
    if remake.brand_kit_id:
        kit = session.get(BrandKit, remake.brand_kit_id)
    if kit is None:
        kit = session.exec(
            _select(BrandKit).where(BrandKit.project_id == remake.project_id, BrandKit.is_default == True)  # noqa: E712
        ).first()
    if kit and kit.logo_s3_key:
        urls.append(_presign(kit.logo_s3_key))

    # Brand-library assets (the columns survive on media_assets even
    # though the old brand_build library UI was removed).
    lib = session.exec(
        _select(MediaAsset)
        .where(
            MediaAsset.project_id == remake.project_id,
            MediaAsset.brand_asset_type.is_not(None),
            MediaAsset.status == "ready",
        )
        .limit(limit)
    ).all()
    for a in lib:
        if len(urls) >= limit:
            break
        if a.s3_key:
            urls.append(_presign(a.s3_key))
    return urls


def _upload_ai_output(remake: Remake, url: str, kind: str, idx: int, ext: str = "mp4") -> str:
    work = common.tempdir()
    try:
        local = _download_output(url, work, f"{kind}.{ext}")
        with open(local, "rb") as fh:
            data = fh.read()
        out_key = s3lib.make_key(remake.project_id, "scenes", f"{remake.id}-{kind}-{idx:02d}.{ext}")
        s3lib.upload_bytes(out_key, data, content_type=f"video/mp4" if ext == "mp4" else "image/png")
        return out_key
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def _restyle(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """Kling v2v re-shoot: keep the shot's motion/composition, swap to our
    brand. Consumes the cut clip; produces a clip for `normalize`."""
    shot = session.get(RemakeShot, step.shot_id) if step.shot_id else None
    if shot is None:
        raise RuntimeError("restyle step has no shot")
    src_key = common.prev_output_key(session, step)
    if not src_key:
        raise RuntimeError("restyle has no upstream cut clip")

    routes = _routes(session, "shot_restyle", remake.project_id)
    video_url = _presign(src_key)
    refs = _brand_ref_urls(session, remake)
    prompt = (shot.prompt or "restyle this shot with our brand, keep the motion and composition").strip()

    def body_for(route: ModelRoute) -> dict:
        body = {"video_url": video_url, "prompt": prompt, **(route.params or {})}
        if refs:
            body["image_urls"] = refs  # @Image refs (logo/product)
        return body

    def on_fail(route: ModelRoute, exc: Exception) -> None:
        calls_svc.record(
            session, project_id=remake.project_id, task_key="shot_restyle",
            provider="fal", model_id=route.model_id, remake_id=remake.id,
            remake_shot_id=shot.id, status_="failed", error=str(exc)[:500],
        )

    result = fal_queue.run_task(routes, body_for, timeout_seconds=1200, on_attempt_failed=on_fail)
    if not result.output_url:
        raise RuntimeError(f"restyle returned no video url ({result.model_id})")
    out_key = _upload_ai_output(remake, result.output_url, "restyle", shot.idx)
    calls_svc.record(
        session, project_id=remake.project_id, task_key="shot_restyle",
        provider="fal", model_id=result.model_id, remake_id=remake.id,
        remake_shot_id=shot.id, latency_ms=result.latency_ms, cost_usd=shot.est_cost_usd or 0.0,
    )
    return {"s3_key": out_key}


def _keyframe_edit(which: str):
    """Handler factory for the start / end keyframe edits (reframe).

    Edits one of the shot's keyframes to swap in our product/logo via
    nano-banana with brand reference images."""
    frame_pos = "start" if which == "start" else "end"

    def _h(session: Session, step: RemakeStep, remake: Remake) -> dict:
        shot = session.get(RemakeShot, step.shot_id) if step.shot_id else None
        if shot is None:
            raise RuntimeError("keyframe_edit step has no shot")
        frames = shot.frames or {}
        base_key = frames.get(frame_pos) or frames.get("mid") or frames.get("start")
        if not base_key:
            raise RuntimeError(f"no {frame_pos} frame to edit for shot {shot.idx}")

        routes = _routes(session, "keyframe_edit", remake.project_id)
        image_url = _presign(base_key)
        refs = _brand_ref_urls(session, remake)
        prompt = (shot.prompt or "replace the product/logo with ours, keep the framing").strip()

        def body_for(route: ModelRoute) -> dict:
            body = {"image_url": image_url, "prompt": prompt, **(route.params or {})}
            if refs:
                body["reference_image_urls"] = refs
            return body

        result = fal_queue.run_task(routes, body_for, timeout_seconds=600)
        if not result.output_url:
            raise RuntimeError(f"keyframe_edit returned no image url ({result.model_id})")
        out_key = _upload_ai_output(remake, result.output_url, f"kf-{frame_pos}", shot.idx, ext="png")
        calls_svc.record(
            session, project_id=remake.project_id, task_key="keyframe_edit",
            provider="fal", model_id=result.model_id, remake_id=remake.id,
            remake_shot_id=shot.id, image_count=1, latency_ms=result.latency_ms,
        )
        return {"s3_key": out_key}

    return _h


def _i2v(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """Animate the two edited keyframes into a clip (start+end frame i2v).
    Consumes both keyframe_edit outputs; produces a clip for `normalize`."""
    shot = session.get(RemakeShot, step.shot_id) if step.shot_id else None
    if shot is None:
        raise RuntimeError("i2v step has no shot")
    start_key = common.shot_step_output(session, shot.id, "keyframe_edit_start")
    end_key = common.shot_step_output(session, shot.id, "keyframe_edit_end")
    if not start_key:
        raise RuntimeError("i2v has no start keyframe")

    routes = _routes(session, "shot_i2v", remake.project_id)
    duration = max(1, round(float(shot.end_sec) - float(shot.start_sec)))
    start_url = _presign(start_key)
    end_url = _presign(end_key) if end_key else None
    prompt = (shot.prompt or "animate naturally").strip()

    def body_for(route: ModelRoute) -> dict:
        body = {"image_url": start_url, "prompt": prompt, "duration": duration, **(route.params or {})}
        if end_url:
            body["end_image_url"] = end_url
        return body

    result = fal_queue.run_task(routes, body_for, timeout_seconds=1200)
    if not result.output_url:
        raise RuntimeError(f"i2v returned no video url ({result.model_id})")
    out_key = _upload_ai_output(remake, result.output_url, "i2v", shot.idx)
    calls_svc.record(
        session, project_id=remake.project_id, task_key="shot_i2v",
        provider="fal", model_id=result.model_id, remake_id=remake.id,
        remake_shot_id=shot.id, video_seconds=duration, latency_ms=result.latency_ms,
        cost_usd=shot.est_cost_usd or 0.0,
    )
    return {"s3_key": out_key}


def _not_wired(kind: str):
    def _h(session: Session, step: RemakeStep, remake: Remake) -> dict:
        raise RuntimeError(f"{kind} is not wired yet")
    return _h


_HANDLERS = {
    "asr": _asr,
    "erase": _erase,
    "restyle": _restyle,
    "keyframe_edit_start": _keyframe_edit("start"),
    "keyframe_edit_end": _keyframe_edit("end"),
    "i2v": _i2v,
    # Phase 3 — audio.
    "tts": _not_wired("tts"),
    "lipsync": _not_wired("lipsync"),
    "upscale": _not_wired("upscale"),
}


def run(step_id: str) -> dict:
    return common.run_step(step_id, _HANDLERS)
