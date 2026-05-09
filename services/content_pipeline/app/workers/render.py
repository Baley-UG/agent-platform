"""media_render RQ task — compose one render_variant.

Entry point: `app.workers.render.run(variant_id)`.

Gathers the scene videos for the variant's aspect_group + the scenario's
voiceover + music_track, calls the ffmpeg renderer, writes a versioned
`media_assets` row of type `final_video`, links
`render_variant.final_asset_id`, records `generation_calls` for the
compose work (provider='self_ffmpeg', cost_usd=0 — we don't bill ffmpeg),
and rolls up `scenarios.status`.
"""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from sqlmodel import select

from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.music import MusicTrack
from app.models.render_variants import RenderVariant
from app.models.scenarios import Scenario
from app.models.scene_renders import SceneRender
from app.services import generation_calls as calls_svc
from app.services import media_assets as media_svc
from app.services import render_variants as variants_svc
from app.services import renderer as renderer_svc
from app.services.database import session_scope
from app.services.presets import PRESETS


def _scene_video_keys_for_variant(session, scenario: Scenario, preset_key: str) -> List[str]:
    """Return the scene video S3 keys for the variant's aspect group, in scene order."""
    preset = PRESETS[preset_key]
    aspect = preset.aspect

    rows = session.exec(
        select(SceneRender)
        .where(SceneRender.scenario_id == scenario.id, SceneRender.aspect_ratio == aspect)
        .order_by(SceneRender.scene_idx)
    ).all()

    keys: List[str] = []
    for r in rows:
        if r.video_asset_id is None:
            raise renderer_svc.FFmpegError(
                f"scene {r.scene_idx} for aspect={aspect} has no video_asset_id; cannot compose"
            )
        asset = session.get(MediaAsset, r.video_asset_id)
        if asset is None:
            raise renderer_svc.FFmpegError(f"video asset {r.video_asset_id} not found")
        keys.append(asset.s3_key)
    if not keys:
        raise renderer_svc.FFmpegError(f"no scene_renders found for aspect={aspect}")
    return keys


def _voiceover_key(session, scenario: Scenario) -> Optional[str]:
    if scenario.voiceover_asset_id is None:
        return None
    asset = session.get(MediaAsset, scenario.voiceover_asset_id)
    return asset.s3_key if asset else None


def _music_key(session, scenario: Scenario) -> Optional[str]:
    if scenario.music_track_id is None:
        return None
    track = session.get(MusicTrack, scenario.music_track_id)
    return track.audio_s3_key if track else None


def run(variant_id: str) -> dict:
    variant_uuid = uuid.UUID(variant_id)

    with session_scope() as session:
        variant = session.get(RenderVariant, variant_uuid)
        if variant is None:
            logger.warning("render_variant_missing", variant_id=variant_id)
            return {"ok": False, "error": "render_variant not found"}

        scenario = session.get(Scenario, variant.scenario_id)
        if scenario is None:
            variants_svc.mark_failed(session, variant, "scenario missing")
            return {"ok": False, "error": "scenario missing"}

        # Gather inputs.
        try:
            scene_keys = _scene_video_keys_for_variant(session, scenario, variant.preset_key)
        except renderer_svc.FFmpegError as exc:
            variants_svc.mark_failed(session, variant, str(exc))
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            return {"ok": False, "error": str(exc)}

        inputs = renderer_svc.ComposeInputs(
            scene_video_keys=scene_keys,
            voiceover_key=_voiceover_key(session, scenario),
            music_key=_music_key(session, scenario),
        )

        variants_svc.mark_composing(session, variant)
        started = time.monotonic()
        try:
            output = renderer_svc.compose_variant(
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                preset_key=variant.preset_key,
                inputs=inputs,
                output_filename=f"scenario-{scenario.id}-{variant.preset_key}-v{variant.id}.mp4",
            )
        except renderer_svc.FFmpegError as exc:
            logger.warning("compose_failed", variant_id=variant_id, error=str(exc))
            variants_svc.mark_failed(session, variant, str(exc))
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                variant_id=variant.id,
                task_key="compose",
                provider="self_ffmpeg",
                model_id="ffmpeg",
                status_="failed",
                error=str(exc)[:1000],
                cost_usd=0.0,
            )
            return {"ok": False, "error": str(exc)}

        latency_ms = int((time.monotonic() - started) * 1000)

        # Versioned media_assets write.
        prior_id = variant.final_asset_id
        prior: Optional[MediaAsset] = session.get(MediaAsset, prior_id) if prior_id else None
        metadata = {
            "preset_key": variant.preset_key,
            "render_recipe": output["recipe"],
            "ffmpeg_cmd": output["ffmpeg_cmd"],
        }
        if prior is not None:
            new_asset = media_svc.replace(
                session,
                prior,
                s3_key=output["s3_key"],
                mime_type="video/mp4",
                size_bytes=output["file_size_bytes"],
                width=PRESETS[variant.preset_key].width,
                height=PRESETS[variant.preset_key].height,
                metadata=metadata,
            )
        else:
            new_asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="final_video",
                s3_key=output["s3_key"],
                mime_type="video/mp4",
                size_bytes=output["file_size_bytes"],
                width=PRESETS[variant.preset_key].width,
                height=PRESETS[variant.preset_key].height,
                parent_scenario_id=scenario.id,
                metadata=metadata,
            )

        variants_svc.mark_ready(
            session,
            variant,
            final_asset=new_asset,
            file_size_bytes=output["file_size_bytes"],
            render_recipe=output["recipe"],
        )

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            variant_id=variant.id,
            task_key="compose",
            provider="self_ffmpeg",
            model_id="ffmpeg",
            cost_usd=0.0,
            latency_ms=latency_ms,
            status_="success",
        )

        variants_svc.recompute_scenario_status_from_variants(session, scenario)

        return {
            "ok": True,
            "variant_id": str(variant.id),
            "asset_id": str(new_asset.id),
            "version": new_asset.version,
            "file_size_bytes": output["file_size_bytes"],
        }
