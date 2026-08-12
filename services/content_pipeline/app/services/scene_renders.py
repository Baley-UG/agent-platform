"""scene_renders service — fan-out across (scene, aspect_group), state rollup, regenerate.

Core flows:
- `materialize_for_scenario(scenario)` — called when a scenario is approved.
  Walks `scenario.scenario_json['scenes']` × `scenario.target_aspect_groups`
  and inserts one row per pair (idempotent, ON CONFLICT DO NOTHING via
  pre-check).
- `mark_image_ready(...)` / `mark_failed(...)` — worker callbacks.
- `recompute_scenario_status_from_renders(...)` — rolls up scene_render
  statuses into the parent `scenarios.status` (approved → generating_images
  → images_ready or failed).
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.media_assets import MediaAsset
from app.models.scene_renders import SceneRender
from app.models.scenarios import Scenario
from app.services import scenarios as scenarios_svc


def _scene_count(scenario: Scenario) -> int:
    if not scenario.scenario_json:
        return 0
    scenes = scenario.scenario_json.get("scenes") or []
    return len(scenes)


def _aspect_groups_for(scenario: Scenario) -> List[str]:
    return list(scenario.target_aspect_groups or [])


def expected_render_count(scenario: Scenario) -> int:
    return _scene_count(scenario) * len(_aspect_groups_for(scenario))


def materialize_for_scenario(session: Session, scenario: Scenario) -> List[SceneRender]:
    """Insert scene_renders rows for every (scene_idx, aspect_group) pair.

    Idempotent: rows that already exist for this (scenario, scene_idx,
    aspect_ratio) tuple are left alone.
    """
    if scenario.scenario_json is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="scenario_json is empty — cannot materialize renders"
        )
    aspect_groups = _aspect_groups_for(scenario)
    if not aspect_groups:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="scenario has no target_aspect_groups"
        )

    scenes = scenario.scenario_json.get("scenes") or []
    if not scenes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="scenario_json.scenes is empty"
        )

    existing_keys = {
        (r.scene_idx, r.aspect_ratio)
        for r in session.exec(
            select(SceneRender).where(SceneRender.scenario_id == scenario.id)
        ).all()
    }

    # Phase 4 — resolve the per-scene init image S3 keys from the
    # source reference once, then stamp them onto each new render.
    # `compute_init_keys` returns one entry per scene_idx; we map by
    # `scene.get("idx")` so the order in `scenario_json.scenes` drives
    # the assignment (even when idx values are 1-based or sparse).
    init_keys_by_scene: dict[int, Optional[str]] = {}
    try:
        from app.services import reference_frames as ref_frames_svc
        from app.models.content_references import ContentReference

        if scenario.reference_id:
            reference = session.get(ContentReference, scenario.reference_id)
            if reference is not None:
                scene_indices = [
                    s.get("idx") for s in scenes if isinstance(s, dict) and s.get("idx") is not None
                ]
                init_keys = ref_frames_svc.compute_init_keys(reference, len(scene_indices))
                for idx_pos, scene_idx in enumerate(scene_indices):
                    init_keys_by_scene[scene_idx] = (
                        init_keys[idx_pos] if idx_pos < len(init_keys) else None
                    )
    except Exception:  # noqa: BLE001
        # Never block render materialization on init-key resolution.
        # Empty map → renders fall back to pure t2i, same as today.
        init_keys_by_scene = {}

    # Default img2img strength applied at materialize time. Director
    # later overrides per-cell via `image_strength`; admins override
    # per-route via `model_routes.params.image_strength`. The stamp
    # here just gives the image_gen worker a non-null fallback so it
    # can choose img2img instead of t2i.
    try:
        from app.services.reference_frames import DEFAULT_REFERENCE_STRENGTH
    except Exception:  # noqa: BLE001
        DEFAULT_REFERENCE_STRENGTH = 0.55  # type: ignore

    created: List[SceneRender] = []
    for scene in scenes:
        idx = scene.get("idx")
        if idx is None:
            continue
        init_key = init_keys_by_scene.get(idx)
        for aspect in aspect_groups:
            if (idx, aspect) in existing_keys:
                continue
            row = SceneRender(
                scenario_id=scenario.id,
                scene_idx=idx,
                aspect_ratio=aspect,
                status="pending",
                init_image_s3_key=init_key,
                image_strength=(
                    DEFAULT_REFERENCE_STRENGTH if init_key else None
                ),
            )
            session.add(row)
            created.append(row)
    if created:
        session.flush()
    return created


def list_for_scenario(session: Session, scenario_id: uuid.UUID) -> List[SceneRender]:
    return list(
        session.exec(
            select(SceneRender)
            .where(SceneRender.scenario_id == scenario_id)
            .order_by(SceneRender.scene_idx, SceneRender.aspect_ratio)
        ).all()
    )


def get(session: Session, render_id: uuid.UUID) -> SceneRender:
    row = session.get(SceneRender, render_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="scene_render not found")
    return row


def get_for_scene(
    session: Session, scenario_id: uuid.UUID, scene_idx: int, aspect_ratio: str
) -> SceneRender:
    stmt = select(SceneRender).where(
        SceneRender.scenario_id == scenario_id,
        SceneRender.scene_idx == scene_idx,
        SceneRender.aspect_ratio == aspect_ratio,
    )
    row = session.exec(stmt).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scene_render not found: scene_idx={scene_idx} aspect={aspect_ratio}",
        )
    return row


# ---------- worker callbacks ----------


def mark_generating_image(session: Session, render: SceneRender) -> SceneRender:
    render.status = "generating_image"
    render.error = None
    session.add(render)
    session.flush()
    return render


def mark_image_ready(session: Session, render: SceneRender, asset: MediaAsset) -> SceneRender:
    render.image_asset_id = asset.id
    render.status = "image_ready"
    render.error = None
    session.add(render)
    session.flush()
    return render


def mark_generating_video(session: Session, render: SceneRender) -> SceneRender:
    render.status = "generating_video"
    render.error = None
    session.add(render)
    session.flush()
    return render


def mark_video_ready(session: Session, render: SceneRender, asset: MediaAsset) -> SceneRender:
    render.video_asset_id = asset.id
    render.status = "video_ready"
    render.error = None
    session.add(render)
    session.flush()
    return render


def mark_failed(session: Session, render: SceneRender, error: str) -> SceneRender:
    render.status = "failed"
    render.error = error[:2000]
    session.add(render)
    session.flush()
    return render


# ---------- scenario status roll-up ----------


def recompute_scenario_status_from_renders(session: Session, scenario: Scenario) -> Scenario:
    """Walk scene_renders for this scenario and bump scenario.status accordingly.

    Rules:
    - Any render `failed` AND scenario currently in a generating_* state → scenario `failed`.
    - All renders `image_ready` AND scenario `generating_images` → `images_ready`.
    - All renders `video_ready` AND scenario `generating_videos` → `videos_ready`.
    """
    rows = list_for_scenario(session, scenario.id)
    if not rows:
        return scenario

    statuses = {r.status for r in rows}

    if "failed" in statuses and scenario.status in ("generating_images", "generating_videos"):
        scenarios_svc.mark_failed(
            session, scenario, "one or more scene_renders failed (see scene_renders.error)"
        )
        return scenario

    if scenario.status == "generating_images" and statuses == {"image_ready"}:
        scenarios_svc.transition(scenario, "images_ready")
        session.add(scenario)
        session.flush()
    elif scenario.status == "generating_videos" and statuses == {"video_ready"}:
        scenarios_svc.transition(scenario, "videos_ready")
        session.add(scenario)
        session.flush()

    return scenario


# ---------- regenerate-image ----------


def claim_for_video_regenerate(
    session: Session, scenario_id: uuid.UUID, scene_idx: int, aspect_ratio: str
) -> SceneRender:
    """Pre-flight for `POST /scenes/{idx}/regenerate-video`."""
    render = get_for_scene(session, scenario_id, scene_idx, aspect_ratio)
    if render.image_asset_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="scene_render has no image yet; generate / approve images first",
        )
    if render.status == "generating_video":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="scene_render is already generating_video; wait or fail it first",
        )
    return render


def renders_with_video_pending(session: Session, scenario_id: uuid.UUID) -> List[SceneRender]:
    """Renders that are `image_ready` and need an I2V job."""
    return [
        r
        for r in list_for_scenario(session, scenario_id)
        if r.status == "image_ready" and r.image_asset_id is not None
    ]


def claim_for_image_regenerate(
    session: Session, scenario_id: uuid.UUID, scene_idx: int, aspect_ratio: str
) -> SceneRender:
    """Pre-flight for `POST /scenes/{idx}/regenerate-image`. Returns the row to (re)render."""
    render = get_for_scene(session, scenario_id, scene_idx, aspect_ratio)
    if render.status in ("generating_image", "generating_video"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"scene_render is already in status={render.status}; wait or fail it first",
        )
    return render
