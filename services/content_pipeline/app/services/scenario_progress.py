"""Aggregate scenario progress — single read for the admin panel.

The panel currently polls 4 endpoints (scenario, scene-renders,
render-variants, generation-calls) every few seconds. This service rolls
them into a single GET so the panel can poll one URL for the full
pipeline state.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.logging import logger
from app.models.generation_calls import GenerationCall
from app.models.media_assets import MediaAsset
from app.models.render_variants import RenderVariant
from app.models.scene_renders import SceneRender
from app.models.scenarios import Scenario
from app.services import scenarios as scenarios_svc


def _signed_url(s3_key: str) -> str | None:
    """Short-lived presigned GET. Returns None on failure rather than
    raising — the panel falls back to "no thumb"."""
    try:
        return s3lib.presigned_get_url(s3_key, ttl=900)
    except Exception as exc:  # noqa: BLE001
        logger.warning("progress_presign_failed", key=s3_key, error=str(exc))
        return None


def _segment_plan_payload(scenario: Scenario) -> Optional[dict]:
    """The stored cut list plus presigned frame thumbs and the
    source-footage ratio. Returns None for non-repurpose scenarios."""
    plan = scenario.segment_plan
    if not plan:
        return None
    from app.services import segments as segments_svc

    enriched = []
    for seg in plan.get("segments") or []:
        key = seg.get("frame_s3_key")
        enriched.append({**seg, "frame_url": _signed_url(key) if key else None})
    return {
        **plan,
        "segments": enriched,
        "source_ratio": segments_svc.source_ratio(plan),
        "total_duration_sec": segments_svc.total_duration_sec(plan),
    }


def build(session: Session, scenario: Scenario) -> dict:
    """Return one dict with everything the admin panel needs to render the
    scenario detail page.
    """
    # scene_renders grouped by scene_idx
    renders = list(
        session.exec(
            select(SceneRender)
            .where(SceneRender.scenario_id == scenario.id)
            .order_by(SceneRender.scene_idx, SceneRender.aspect_ratio)
        ).all()
    )
    by_scene: dict[int, list] = {}
    for r in renders:
        by_scene.setdefault(r.scene_idx, []).append(
            {
                "id": str(r.id),
                "aspect_ratio": r.aspect_ratio,
                "image_asset_id": str(r.image_asset_id) if r.image_asset_id else None,
                "video_asset_id": str(r.video_asset_id) if r.video_asset_id else None,
                # Phase 2 — director output. When set, the image_gen
                # worker bypasses synthesis and reuses this brand asset.
                "resolved_asset_id": str(r.resolved_asset_id) if r.resolved_asset_id else None,
                "match_reason": r.match_reason,
                # Phase 2.5 — img2img remix strength. None = legacy
                # synth path. <=0.15 = pure passthrough. >0.15 = i2i.
                "image_strength": (
                    float(r.image_strength) if r.image_strength is not None else None
                ),
                # Phase 4 — surface the init image so the panel can
                # render a small thumb on the scene card ("seeded from
                # this reference frame"). Server-side presign keeps the
                # browser from making N extra HTTP calls.
                "init_image_s3_key": r.init_image_s3_key,
                "init_image_url": (
                    _signed_url(r.init_image_s3_key)
                    if r.init_image_s3_key
                    else None
                ),
                # Repurpose — the source-video window this cell was cut
                # from, so the panel can label each card "0.00–2.40s"
                # and offer a keep/replace/drop toggle per segment.
                "source_start_sec": (
                    float(r.source_start_sec) if r.source_start_sec is not None else None
                ),
                "source_end_sec": (
                    float(r.source_end_sec) if r.source_end_sec is not None else None
                ),
                "segment_action": r.segment_action,
                "status": r.status,
                "error": r.error,
            }
        )
    scenes = sorted(
        ({"scene_idx": idx, "renders": rows} for idx, rows in by_scene.items()),
        key=lambda x: x["scene_idx"],
    )

    # render_variants
    variants = list(
        session.exec(
            select(RenderVariant)
            .where(RenderVariant.scenario_id == scenario.id)
            .order_by(RenderVariant.preset_key)
        ).all()
    )
    variants_payload = [
        {
            "id": str(v.id),
            "preset_key": v.preset_key,
            "status": v.status,
            "final_asset_id": str(v.final_asset_id) if v.final_asset_id else None,
            # Multi-image carousel variants expose every slide here.
            # Single-asset variants still echo `final_asset_id` as a
            # 1-element list so the panel can always read from this
            # field uniformly.
            "final_asset_ids": [str(x) for x in (v.final_asset_ids or [])] or None,
            "thumbnail_asset_id": str(v.thumbnail_asset_id) if v.thumbnail_asset_id else None,
            "duration_sec": float(v.duration_sec) if v.duration_sec else None,
            "file_size_bytes": v.file_size_bytes,
            "error": v.error,
            "approved_at": v.approved_at.isoformat() if v.approved_at else None,
        }
        for v in variants
    ]

    # voiceover summary (if any)
    voiceover_payload = None
    if scenario.voiceover_asset_id:
        va = session.get(MediaAsset, scenario.voiceover_asset_id)
        if va:
            voiceover_payload = {
                "id": str(va.id),
                "version": va.version,
                "duration_sec": float(va.duration_sec) if va.duration_sec else None,
                "size_bytes": va.size_bytes,
            }

    # cost summary for this scenario only
    cost_stmt = select(
        func.coalesce(func.sum(GenerationCall.cost_usd), 0),
        func.count(GenerationCall.id),
        func.count().filter(GenerationCall.status == "success"),
        func.count().filter(GenerationCall.status != "success"),
    ).where(GenerationCall.scenario_id == scenario.id)
    total_cost, total_calls, success_calls, failed_calls = session.exec(cost_stmt).one()

    # progress summary numbers
    expected_renders = len(scenes) * len(scenario.target_aspect_groups or [])
    image_ready = sum(1 for r in renders if r.status in ("image_ready", "generating_video", "video_ready"))
    video_ready = sum(1 for r in renders if r.status == "video_ready")
    variants_ready = sum(1 for v in variants if v.status in ("ready", "approved", "published"))

    return {
        "scenario": {
            "id": str(scenario.id),
            "project_id": str(scenario.project_id),
            "reference_id": str(scenario.reference_id) if scenario.reference_id else None,
            "status": scenario.status,
            "version": scenario.version,
            # The analyzer-produced document (cta, hook, scenes, music,
            # duration_sec, outro_template_id). Until this was included,
            # the panel saw status=pending_review with no script content
            # and looked like the analysis hadn't run.
            "scenario_json": scenario.scenario_json,
            "production_mode": scenario.production_mode,
            # Repurpose cut list. The panel's segment editor renders
            # from this; `source_ratio` is how much of the output is
            # verbatim source footage.
            "segment_plan": _segment_plan_payload(scenario),
            "target_variants": list(scenario.target_variants or []),
            "target_aspect_groups": list(scenario.target_aspect_groups or []),
            "quality_tier": scenario.quality_tier,
            "generation_cost_usd": float(scenario.generation_cost_usd or 0),
            "default_caption": scenario.default_caption,
            "default_hashtags": list(scenario.default_hashtags or []),
            "voiceover_asset_id": str(scenario.voiceover_asset_id) if scenario.voiceover_asset_id else None,
            "music_track_id": str(scenario.music_track_id) if scenario.music_track_id else None,
            "last_error": scenario.last_error,
            "created_by": scenario.created_by,
            "created_at": scenario.created_at.isoformat() if scenario.created_at else None,
            "updated_at": scenario.updated_at.isoformat() if scenario.updated_at else None,
        },
        "scenes": scenes,
        "variants": variants_payload,
        "voiceover": voiceover_payload,
        "progress": {
            "expected_renders": expected_renders,
            "image_ready": image_ready,
            "video_ready": video_ready,
            "variants_ready": variants_ready,
            "variants_total": len(variants_payload),
        },
        "cost": {
            "total_cost_usd": float(total_cost or 0),
            "total_calls": int(total_calls or 0),
            "success_calls": int(success_calls or 0),
            "failed_calls": int(failed_calls or 0),
        },
        # Action matrix — which start_* buttons the panel should show.
        # See `scenarios_svc.pipeline_actions` for the truth table; in
        # short, photo/carousel sources skip video + audio steps.
        "actions": scenarios_svc.pipeline_actions(session, scenario),
    }
