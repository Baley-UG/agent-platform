"""Aggregate scenario progress — single read for the admin panel.

The panel currently polls 4 endpoints (scenario, scene-renders,
render-variants, generation-calls) every few seconds. This service rolls
them into a single GET so the panel can poll one URL for the full
pipeline state.
"""

from __future__ import annotations

import uuid
from typing import List

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.generation_calls import GenerationCall
from app.models.media_assets import MediaAsset
from app.models.render_variants import RenderVariant
from app.models.scene_renders import SceneRender
from app.models.scenarios import Scenario


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
    }
