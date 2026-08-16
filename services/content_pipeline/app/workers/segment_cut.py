"""RQ task — cut real segments out of a source reel (repurpose mode).

Entry points:
  - `app.workers.segment_cut.run(scenario_id, aspect_ratio)` — cuts every
    `keep` segment for one aspect group in a single ffmpeg invocation.
  - `app.workers.segment_cut.run_one(scene_render_id)` — re-cuts a single
    cell after an admin edits its boundaries.

The produced clips land in `scene_renders.video_asset_id` — the same
slot Seedance writes in recreate mode — so `render.py`, the progress
endpoint, the regenerate endpoints and the status roll-up all work
unchanged.

Runs in the **render container** (ffmpeg-bound).
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from sqlmodel import select

from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.scenarios import Scenario
from app.models.scene_renders import SceneRender
from app.services import generation_calls as calls_svc
from app.services import media_assets as media_svc
from app.services import scene_renders as renders_svc
from app.services import segment_cutter as cutter
from app.services import segments as segments_svc
from app.services.database import session_scope

SEGMENT_ASSET_TYPE = "segment_video"


def _plan_for(scenario: Scenario) -> dict:
    return dict(scenario.segment_plan or {})


def _prior_asset(session, render: SceneRender) -> Optional[MediaAsset]:
    if render.video_asset_id is None:
        return None
    return session.get(MediaAsset, render.video_asset_id)


def _persist(
    session,
    *,
    scenario: Scenario,
    render: SceneRender,
    result: dict,
    plan: dict,
    reference_meta: dict,
) -> MediaAsset:
    """Write the cut clip as a versioned media_asset and link the render.

    The provenance fields are deliberate: a takedown request has to be
    traceable to the exact source post and time window with one query.
    """
    metadata = {
        "reference_id": plan.get("source_reference_id"),
        "source_media_s3_key": plan.get("source_media_s3_key"),
        "source_url": reference_meta.get("source_url"),
        "source_username": reference_meta.get("source_username"),
        "start_sec": result["start_sec"],
        "end_sec": result["end_sec"],
        # The encoder rounds each cut out to a whole frame; keeping both
        # numbers makes that visible instead of looking like a bug when
        # the composed video runs a few frames long.
        "planned_duration_sec": result.get("planned_duration_sec"),
        "aspect": render.aspect_ratio,
        "fit_mode": plan.get("fit_mode") or "cover",
        "ffmpeg_cmd": result["ffmpeg_cmd"],
    }
    prior = _prior_asset(session, render)
    if prior is not None and prior.type == SEGMENT_ASSET_TYPE:
        return media_svc.replace(
            session,
            prior,
            s3_key=result["s3_key"],
            mime_type="video/mp4",
            size_bytes=result["size_bytes"],
            width=result["width"],
            height=result["height"],
            duration_sec=result["duration_sec"],
            metadata=metadata,
        )
    return media_svc.create_initial(
        session,
        project_id=scenario.project_id,
        type_=SEGMENT_ASSET_TYPE,
        s3_key=result["s3_key"],
        mime_type="video/mp4",
        size_bytes=result["size_bytes"],
        width=result["width"],
        height=result["height"],
        duration_sec=result["duration_sec"],
        parent_scenario_id=scenario.id,
        parent_scene_idx=render.scene_idx,
        metadata=metadata,
    )


def _reference_meta(session, scenario: Scenario) -> dict:
    """Source attribution, pulled once per job."""
    if not scenario.reference_id:
        return {}
    from app.models.content_references import ContentReference

    ref = session.get(ContentReference, scenario.reference_id)
    if ref is None:
        return {}
    meta = dict(ref.metadata_json or {})
    return {
        "source_url": ref.source_url,
        "source_username": meta.get("username"),
    }


def run(scenario_id: str, aspect_ratio: str) -> dict:
    """Cut all `keep` segments for one aspect group."""
    scenario_uuid = uuid.UUID(scenario_id)

    with session_scope() as session:
        scenario = session.get(Scenario, scenario_uuid)
        if scenario is None:
            return {"ok": False, "error": "scenario not found"}

        plan = _plan_for(scenario)
        src_key = plan.get("source_media_s3_key")
        if not src_key:
            return {"ok": False, "error": "segment_plan has no source_media_s3_key"}

        all_segments = segments_svc.plan_from_json(plan)
        keep = [s for s in all_segments if s.action == "keep"]
        if not keep:
            return {"ok": True, "cut": 0, "note": "no keep segments"}

        renders = {
            r.scene_idx: r
            for r in session.exec(
                select(SceneRender).where(
                    SceneRender.scenario_id == scenario_uuid,
                    SceneRender.aspect_ratio == aspect_ratio,
                )
            ).all()
        }
        # Only cut segments that actually have a render cell for this aspect.
        keep = [s for s in keep if s.idx in renders]
        if not keep:
            return {"ok": True, "cut": 0, "note": "no matching scene_renders"}

        for seg in keep:
            renders_svc.mark_cutting_segment(session, renders[seg.idx])

        ref_meta = _reference_meta(session, scenario)
        fit_mode = plan.get("fit_mode") or "cover"

        try:
            results = cutter.cut_segments(
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                src_s3_key=src_key,
                segments=keep,
                aspect=aspect_ratio,
                fit_mode=fit_mode,
            )
        except Exception as exc:  # noqa: BLE001 — ffmpeg / S3 / network
            logger.warning(
                "segment_cut_failed",
                scenario_id=scenario_id,
                aspect=aspect_ratio,
                error=str(exc),
            )
            for seg in keep:
                renders_svc.mark_failed(session, renders[seg.idx], str(exc))
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": str(exc)}

        for result in results:
            render = renders[result["idx"]]
            asset = _persist(
                session,
                scenario=scenario,
                render=render,
                result=result,
                plan=plan,
                reference_meta=ref_meta,
            )
            renders_svc.mark_video_ready(session, render, asset)

        # Ledger row so the cost trace shows the (free) ffmpeg step next
        # to the paid provider calls, same as compose does.
        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            task_key="segment_cut",
            provider="self_ffmpeg",
            model_id="ffmpeg",
            cost_usd=0.0,
            status_="success",
        )

        renders_svc.recompute_scenario_status_from_renders(session, scenario)

        logger.info(
            "segment_cut_persisted",
            scenario_id=scenario_id,
            aspect=aspect_ratio,
            cut=len(results),
        )
        return {"ok": True, "scenario_id": scenario_id, "aspect": aspect_ratio, "cut": len(results)}


def run_one(scene_render_id: str) -> dict:
    """Re-cut a single cell — used after an admin edits its boundaries."""
    render_uuid = uuid.UUID(scene_render_id)

    with session_scope() as session:
        render = session.get(SceneRender, render_uuid)
        if render is None:
            return {"ok": False, "error": "scene_render not found"}
        scenario = session.get(Scenario, render.scenario_id)
        if scenario is None:
            return {"ok": False, "error": "scenario not found"}

        plan = _plan_for(scenario)
        src_key = plan.get("source_media_s3_key")
        if not src_key:
            return {"ok": False, "error": "segment_plan has no source_media_s3_key"}

        matches: List = [
            s for s in segments_svc.plan_from_json(plan) if s.idx == render.scene_idx
        ]
        if not matches:
            return {"ok": False, "error": f"no segment with idx={render.scene_idx}"}
        segment = matches[0]
        if segment.action != "keep":
            return {
                "ok": False,
                "error": f"segment action is '{segment.action}', not 'keep'",
            }

        renders_svc.mark_cutting_segment(session, render)
        try:
            results = cutter.cut_segments(
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                src_s3_key=src_key,
                segments=[segment],
                aspect=render.aspect_ratio,
                fit_mode=plan.get("fit_mode") or "cover",
            )
        except Exception as exc:  # noqa: BLE001
            renders_svc.mark_failed(session, render, str(exc))
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": str(exc)}

        asset = _persist(
            session,
            scenario=scenario,
            render=render,
            result=results[0],
            plan=plan,
            reference_meta=_reference_meta(session, scenario),
        )
        renders_svc.mark_video_ready(session, render, asset)
        renders_svc.recompute_scenario_status_from_renders(session, scenario)
        return {"ok": True, "scene_render_id": scene_render_id, "s3_key": asset.s3_key}
