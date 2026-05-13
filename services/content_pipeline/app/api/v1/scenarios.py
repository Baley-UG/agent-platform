"""scenarios endpoints — create, get, edit, approve, regenerate.

Create is a two-phase op: the row is inserted in `draft`, then the
analyzer worker is enqueued and the response carries `analyzer_job_id` so
the admin panel can poll progress.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlmodel import Session

from app.api.v1.deps import get_project, get_session, require_api_key
from app.core.logging import logger
from app.models.projects import Project
from app.schemas.render_variants import (
    RegenerateVoiceoverRequest,
    RenderVariantRead,
    ReselectMusicRequest,
)
from app.schemas.scenarios import ScenarioCreate, ScenarioRead, ScenarioUpdate
from app.schemas.scene_renders import RegenerateImageRequest, RegenerateVideoRequest, SceneRenderRead
from app.services import queue
from app.services import render_variants as variants_svc
from app.services import scenario_progress as progress_svc
from app.services import scenarios as svc
from app.services import scene_renders as renders_svc

router = APIRouter(
    prefix="/projects/{project_id}/scenarios",
    tags=["scenarios"],
    dependencies=[Depends(require_api_key)],
)


def _enqueue_analyzer(scenario_id: uuid.UUID) -> Optional[str]:
    """Push an analyzer job; tolerate Redis being unavailable in dev/test."""
    try:
        job = queue.enqueue("analyzer", "app.workers.analyzer.run", str(scenario_id))
        return job.id
    except Exception:  # noqa: BLE001
        # We log nothing here; queue.enqueue already increments the metric and
        # the admin panel sees `analyzer_job_id=None` when Redis is down.
        return None


def _enqueue_image_gen(scene_render_id: uuid.UUID, prompt_override: Optional[str] = None) -> Optional[str]:
    try:
        job = queue.enqueue(
            "image_gen", "app.workers.image_gen.run", str(scene_render_id), prompt_override
        )
        return job.id
    except Exception as exc:  # noqa: BLE001
        # Don't 500 — admin panel will see `*_job_id=None` and the
        # downstream worker won't have run. But DO log so the cause
        # (Redis down, queue config mismatch, etc.) isn't invisible.
        logger.exception("enqueue_failed", error=str(exc))
        return None


def _enqueue_video_gen(scene_render_id: uuid.UUID, motion_override: Optional[str] = None) -> Optional[str]:
    try:
        job = queue.enqueue(
            "video_gen", "app.workers.video_gen.run", str(scene_render_id), motion_override
        )
        return job.id
    except Exception as exc:  # noqa: BLE001
        # Don't 500 — admin panel will see `*_job_id=None` and the
        # downstream worker won't have run. But DO log so the cause
        # (Redis down, queue config mismatch, etc.) isn't invisible.
        logger.exception("enqueue_failed", error=str(exc))
        return None


def _enqueue_audio_gen(
    scenario_id: uuid.UUID,
    voice_id_override: Optional[str] = None,
    text_override: Optional[str] = None,
) -> Optional[str]:
    try:
        job = queue.enqueue(
            "audio_gen",
            "app.workers.audio_gen.run",
            str(scenario_id),
            voice_id_override,
            text_override,
        )
        return job.id
    except Exception as exc:  # noqa: BLE001
        # Don't 500 — admin panel will see `*_job_id=None` and the
        # downstream worker won't have run. But DO log so the cause
        # (Redis down, queue config mismatch, etc.) isn't invisible.
        logger.exception("enqueue_failed", error=str(exc))
        return None


def _enqueue_render(variant_id: uuid.UUID) -> Optional[str]:
    try:
        job = queue.enqueue("media_render", "app.workers.render.run", str(variant_id))
        return job.id
    except Exception as exc:  # noqa: BLE001
        # Don't 500 — admin panel will see `*_job_id=None` and the
        # downstream worker won't have run. But DO log so the cause
        # (Redis down, queue config mismatch, etc.) isn't invisible.
        logger.exception("enqueue_failed", error=str(exc))
        return None


@router.post("", response_model=ScenarioRead, status_code=status.HTTP_201_CREATED)
def create(
    payload: ScenarioCreate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Create a draft scenario AND kick the analyzer.

    The analyzer call is best-effort — if Redis is unavailable, the scenario
    is still created in `draft` and the admin can retry via `/analyze`.
    """
    scenario = svc.create(session, project, payload, created_by="api")
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)


@router.post("/{scenario_id}/analyze", response_model=ScenarioRead)
def kick_analyzer(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Enqueue the analyzer for a scenario in `draft` or `analyzing`."""
    scenario = svc.get(session, project.id, scenario_id)
    if scenario.status not in ("draft", "analyzing", "failed"):
        return ScenarioRead.model_validate(scenario)
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)


@router.get("", response_model=List[ScenarioRead])
def list_(
    status_: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[ScenarioRead]:
    return [
        ScenarioRead.model_validate(s)
        for s in svc.list_(session, project.id, status_=status_, limit=limit, offset=offset)
    ]


@router.get("/{scenario_id}", response_model=ScenarioRead)
def get(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.get(session, project.id, scenario_id))


@router.patch("/{scenario_id}", response_model=ScenarioRead)
def update(
    scenario_id: uuid.UUID,
    payload: ScenarioUpdate,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.update(session, project.id, scenario_id, payload))


@router.post("/{scenario_id}/approve", response_model=ScenarioRead)
def approve(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    return ScenarioRead.model_validate(svc.approve(session, project.id, scenario_id))


@router.post("/{scenario_id}/regenerate", response_model=ScenarioRead)
def regenerate(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Snapshot the current scenario_json into `previous_scenario_json`,
    bump `version`, and enqueue the analyzer."""
    scenario = svc.begin_regenerate(session, project.id, scenario_id)
    _enqueue_analyzer(scenario.id)
    return ScenarioRead.model_validate(scenario)


# ----- scene_renders / image generation -----


@router.post("/{scenario_id}/start-images", response_model=ScenarioRead)
def start_images(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Materialize scene_renders × aspect_groups, transition to
    `generating_images`, enqueue an image_gen job per pending render."""
    scenario = svc.start_image_generation(session, project.id, scenario_id)
    renders_svc.materialize_for_scenario(session, scenario)
    pending = [r for r in renders_svc.list_for_scenario(session, scenario.id) if r.status == "pending"]
    for render in pending:
        _enqueue_image_gen(render.id)
    return ScenarioRead.model_validate(scenario)


@router.get("/{scenario_id}/scene-renders", response_model=List[SceneRenderRead])
def list_scene_renders(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[SceneRenderRead]:
    # Ensures the scenario exists & belongs to this project, even though the
    # rows themselves are looked up by scenario_id only.
    svc.get(session, project.id, scenario_id)
    return [SceneRenderRead.model_validate(r) for r in renders_svc.list_for_scenario(session, scenario_id)]


@router.post(
    "/{scenario_id}/scenes/{scene_idx}/regenerate-image",
    response_model=List[SceneRenderRead],
)
def regenerate_scene_image(
    scenario_id: uuid.UUID,
    scene_idx: int,
    payload: RegenerateImageRequest = RegenerateImageRequest(),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[SceneRenderRead]:
    """Regenerate one scene's image. Without `aspect_ratio`, regenerates all
    aspect-group masters for that scene; the new image_gen run produces a
    fresh `media_assets` version, the prior asset chain stays for rollback.
    """
    scenario = svc.get(session, project.id, scenario_id)
    if scenario.status not in ("images_ready", "videos_ready", "audio_ready", "approved", "generating_images", "failed"):
        # Out of these states the regenerate doesn't make sense (we'd be
        # racing the auto fan-out or there's nothing to replace yet).
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"cannot regenerate image while scenario is in status={scenario.status}",
        )

    aspects = (
        [payload.aspect_ratio] if payload.aspect_ratio else list(scenario.target_aspect_groups or [])
    )
    if not aspects:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="scenario has no target_aspect_groups"
        )

    enqueued: List = []
    for aspect in aspects:
        render = renders_svc.claim_for_image_regenerate(session, scenario_id, scene_idx, aspect)
        _enqueue_image_gen(render.id, payload.prompt_override)
        enqueued.append(render)
    return [SceneRenderRead.model_validate(r) for r in enqueued]


# ----- video generation -----


@router.post("/{scenario_id}/start-videos", response_model=ScenarioRead)
def start_videos(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Transition `images_ready → generating_videos` and enqueue an I2V job
    for every scene_render that has an image but no video yet."""
    scenario = svc.start_video_generation(session, project.id, scenario_id)
    pending = renders_svc.renders_with_video_pending(session, scenario.id)
    for render in pending:
        _enqueue_video_gen(render.id)
    return ScenarioRead.model_validate(scenario)


@router.post(
    "/{scenario_id}/scenes/{scene_idx}/regenerate-video",
    response_model=List[SceneRenderRead],
)
def regenerate_scene_video(
    scenario_id: uuid.UUID,
    scene_idx: int,
    payload: RegenerateVideoRequest = RegenerateVideoRequest(),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[SceneRenderRead]:
    """Regenerate one scene's video. Without `aspect_ratio`, regenerates
    ALL aspect-group masters for that scene; with one, just that variant.
    Each run produces a fresh `media_assets` version; the prior video
    asset chain stays for rollback.
    """
    scenario = svc.get(session, project.id, scenario_id)
    if scenario.status not in ("videos_ready", "audio_ready", "generating_videos", "images_ready", "failed"):
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"cannot regenerate video while scenario is in status={scenario.status}",
        )

    aspects = (
        [payload.aspect_ratio] if payload.aspect_ratio else list(scenario.target_aspect_groups or [])
    )
    if not aspects:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail="scenario has no target_aspect_groups"
        )

    enqueued: List = []
    for aspect in aspects:
        render = renders_svc.claim_for_video_regenerate(session, scenario_id, scene_idx, aspect)
        _enqueue_video_gen(render.id, payload.motion_override)
        enqueued.append(render)
    return [SceneRenderRead.model_validate(r) for r in enqueued]


# ----- audio + compose -----


@router.post("/{scenario_id}/start-audio", response_model=ScenarioRead)
def start_audio(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Transition videos_ready → generating_audio and enqueue the TTS job."""
    scenario = svc.start_audio_generation(session, project.id, scenario_id)
    _enqueue_audio_gen(scenario.id)
    return ScenarioRead.model_validate(scenario)


@router.post("/{scenario_id}/regenerate-voiceover", response_model=ScenarioRead)
def regenerate_voiceover(
    scenario_id: uuid.UUID,
    payload: RegenerateVoiceoverRequest = RegenerateVoiceoverRequest(),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Re-run TTS. Versioned media_assets chain — prior voiceover stays for rollback."""
    scenario = svc.start_audio_generation(session, project.id, scenario_id)
    _enqueue_audio_gen(scenario.id, payload.voice_id_override, payload.text_override)
    return ScenarioRead.model_validate(scenario)


@router.post("/{scenario_id}/reselect-music", response_model=ScenarioRead)
def reselect_music(
    scenario_id: uuid.UUID,
    payload: ReselectMusicRequest = ReselectMusicRequest(),
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Set or auto-pick the scenario's music_track_id (no TTS re-run)."""
    from app.models.music import MusicTrack
    from app.services import audio as audio_svc

    scenario = svc.get(session, project.id, scenario_id)
    if payload.music_track_id is not None:
        track = session.get(MusicTrack, payload.music_track_id)
        if track is None or track.project_id != project.id:
            from fastapi import HTTPException, status as http_status

            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="music_track not found in this project",
            )
        scenario.music_track_id = track.id
    else:
        track = audio_svc.select_music_for_scenario(session, scenario)
        scenario.music_track_id = track.id if track else None
    session.add(scenario)
    session.flush()
    session.refresh(scenario)
    return ScenarioRead.model_validate(scenario)


@router.post("/{scenario_id}/start-compose", response_model=ScenarioRead)
def start_compose(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Materialize render_variants and enqueue compose jobs for each."""
    scenario = svc.start_compose(session, project.id, scenario_id)
    variants_svc.materialize_for_scenario(session, scenario)
    enqueued = 0
    for variant in variants_svc.list_for_scenario(session, scenario.id):
        if variant.status in ("pending", "failed"):
            variants_svc.mark_composing(session, variant)
            _enqueue_render(variant.id)
            enqueued += 1
    # If every variant was already `ready`/`approved` (re-entering compose
    # from final_pending_review or approved_final with no actual work to
    # do) the rollup below advances the scenario back out of `composing`
    # immediately, otherwise it would stay stuck — there's no worker
    # job to fire the rollup later.
    if enqueued == 0:
        variants_svc.recompute_scenario_status_from_variants(session, scenario)
        session.refresh(scenario)
    return ScenarioRead.model_validate(scenario)


@router.get("/{scenario_id}/render-variants", response_model=List[RenderVariantRead])
def list_render_variants(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> List[RenderVariantRead]:
    svc.get(session, project.id, scenario_id)  # auth scope
    return [RenderVariantRead.model_validate(v) for v in variants_svc.list_for_scenario(session, scenario_id)]


@router.post("/{scenario_id}/render-variants/{variant_id}/recompose", response_model=RenderVariantRead)
def recompose_variant(
    scenario_id: uuid.UUID,
    variant_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RenderVariantRead:
    """Re-run ffmpeg for a single variant. Scene videos / audio unchanged.

    Cheap because no LLM / fal / Seedance / TTS spend — just CPU time.
    """
    svc.get(session, project.id, scenario_id)  # auth scope
    variant = variants_svc.claim_for_recompose(session, variant_id)
    if variant.scenario_id != scenario_id:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="variant does not belong to that scenario"
        )
    variants_svc.mark_composing(session, variant)
    _enqueue_render(variant.id)
    return RenderVariantRead.model_validate(variant)


@router.post("/{scenario_id}/render-variants/{variant_id}/approve", response_model=RenderVariantRead)
def approve_variant(
    scenario_id: uuid.UUID,
    variant_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> RenderVariantRead:
    svc.get(session, project.id, scenario_id)  # auth scope
    variant = variants_svc.get(session, variant_id)
    if variant.scenario_id != scenario_id:
        from fastapi import HTTPException, status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="variant does not belong to that scenario"
        )
    return RenderVariantRead.model_validate(variants_svc.approve(session, variant))


@router.post("/{scenario_id}/approve-final", response_model=ScenarioRead)
def approve_final_scenario(
    scenario_id: uuid.UUID,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> ScenarioRead:
    """Mark the whole scenario as final-approved (ready for plan / publish)."""
    return ScenarioRead.model_validate(svc.approve_final(session, project.id, scenario_id))


# Scenarios that change state autonomously (a worker will flip them) →
# panel should poll fast. Anything else is admin-action-gated and panel
# can back off until the user clicks something.
_LIVE_STATUSES = frozenset({
    "analyzing",
    "generating_images",
    "generating_videos",
    "generating_audio",
    "composing",
})
_LIVE_POLL_SECONDS = 3
_IDLE_POLL_SECONDS = 60


@router.get("/{scenario_id}/progress")
def get_progress(
    scenario_id: uuid.UUID,
    response: Response,
    project: Project = Depends(get_project),
    session: Session = Depends(get_session),
) -> dict:
    """Aggregate read for the admin panel.

    One GET returns the scenario row, scene_renders grouped by scene_idx,
    render_variants, voiceover summary, progress counters, and per-scenario
    cost summary. Use this instead of polling four endpoints separately.

    The response includes an `X-Poll-Interval-Seconds` header so the
    panel can adapt its refresh cadence:
      - 3s while a worker is mutating the scenario (analyzing, etc.)
      - 60s when waiting on an admin action (pending_review, approved, ...)
    """
    scenario = svc.get(session, project.id, scenario_id)
    payload = progress_svc.build(session, scenario)

    poll = _LIVE_POLL_SECONDS if scenario.status in _LIVE_STATUSES else _IDLE_POLL_SECONDS
    response.headers["X-Poll-Interval-Seconds"] = str(poll)
    # Inline into the JSON too so panels that can't read response headers
    # (cross-origin without explicit expose) still get the hint.
    payload["poll_interval_seconds"] = poll
    payload["is_live"] = scenario.status in _LIVE_STATUSES
    return payload
