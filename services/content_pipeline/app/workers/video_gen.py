"""video_gen RQ task — image-to-video.

Entry point: `app.workers.video_gen.run(scene_render_id, motion_override=None)`.

Reads the scene's `image_asset_id`, presigns a short-lived GET URL for
the provider, hands it (plus the scene's `motion_prompt` and
`scene.duration`) to the resolved video provider, persists the bytes to
S3, writes a versioned `media_assets` row, links it on the scene_render,
and rolls up the scenario status.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.core import s3
from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.scenarios import Scenario
from app.models.scene_renders import SceneRender
from app.services import generation_calls as calls_svc
from app.services import media_assets as media_svc
from app.services import model_router
from app.services import scene_renders as renders_svc
from app.services.database import session_scope
from app.services.providers.video.base import VideoProvider
from app.services.providers.video.seedance_fal import SeedanceFalProvider


def _build_provider(provider_name: str) -> VideoProvider:
    if provider_name == "seedance":
        return SeedanceFalProvider()
    raise NotImplementedError(f"video provider not yet implemented: {provider_name}")


def _scene_for_idx(scenario: Scenario, scene_idx: int) -> Optional[dict]:
    if not scenario.scenario_json:
        return None
    for scene in scenario.scenario_json.get("scenes") or []:
        if scene.get("idx") == scene_idx:
            return scene
    return None


def _motion_prompt(scene: dict) -> str:
    motion = (scene.get("motion_prompt") or "").strip()
    if motion:
        return motion
    # Fall back to image_prompt + a generic motion hint so the provider
    # always receives something workable.
    base = (scene.get("image_prompt") or "").strip()
    return f"{base} — slow gentle motion, cinematic" if base else "slow gentle motion, cinematic"


def _scene_duration(scene: dict, fallback: float = 5.0) -> float:
    duration = scene.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    return fallback


def run(scene_render_id: str, motion_override: Optional[str] = None) -> dict:
    render_uuid = uuid.UUID(scene_render_id)

    with session_scope() as session:
        render = session.get(SceneRender, render_uuid)
        if render is None:
            logger.warning("video_gen_render_missing", scene_render_id=scene_render_id)
            return {"ok": False, "error": "scene_render not found"}

        if render.image_asset_id is None:
            renders_svc.mark_failed(session, render, "no image_asset_id; cannot run I2V")
            return {"ok": False, "error": "no image asset"}

        scenario = session.get(Scenario, render.scenario_id)
        if scenario is None or scenario.scenario_json is None:
            renders_svc.mark_failed(session, render, "scenario or scenario_json missing")
            return {"ok": False, "error": "scenario missing"}

        scene = _scene_for_idx(scenario, render.scene_idx)
        if scene is None:
            renders_svc.mark_failed(session, render, f"scene_idx {render.scene_idx} not in scenario_json")
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": "scene missing"}

        prompt = motion_override or _motion_prompt(scene)
        duration_sec = _scene_duration(scene)

        try:
            route = model_router.resolve(session, "scene_video", project_id=scenario.project_id)
        except model_router.NoRouteError as exc:
            renders_svc.mark_failed(session, render, f"no I2V route: {exc}")
            return {"ok": False, "error": str(exc)}

        # Presign a GET URL for the source image so the provider can fetch it.
        image_asset = session.get(MediaAsset, render.image_asset_id)
        if image_asset is None:
            renders_svc.mark_failed(session, render, "image_asset_id points at a missing media_assets row")
            return {"ok": False, "error": "image asset row missing"}
        try:
            image_url = s3.presigned_get_url(image_asset.s3_key)
        except Exception as exc:  # noqa: BLE001
            renders_svc.mark_failed(session, render, f"presign failed: {exc}")
            return {"ok": False, "error": "presign failed"}

        renders_svc.mark_generating_video(session, render)
        provider = _build_provider(route.provider)

        try:
            response = asyncio.run(
                provider.generate(
                    image_url=image_url,
                    prompt=prompt,
                    route=route,
                    duration_sec=duration_sec,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("video_gen_call_failed", scene_render_id=scene_render_id, error=str(exc))
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                scene_idx=render.scene_idx,
                task_key="scene_video",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            renders_svc.mark_failed(session, render, str(exc))
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": str(exc)}

        # Upload bytes to S3.
        ext = "mp4" if response.mime_type.endswith("mp4") else "bin"
        key = s3.make_key(
            scenario.project_id,
            "scenes",
            f"scenario-{scenario.id}-scene-{render.scene_idx}-{render.aspect_ratio.replace(':', 'x')}.{ext}",
        )
        s3.upload_bytes(key, response.video_bytes, content_type=response.mime_type)

        prior_video_id = render.video_asset_id
        prior: Optional[MediaAsset] = session.get(MediaAsset, prior_video_id) if prior_video_id else None
        metadata = {
            "aspect_ratio": render.aspect_ratio,
            "model_id": route.model_id,
            "provider": route.provider,
            "raw": response.raw,
            "prompt": prompt,
            "source_image_asset_id": str(image_asset.id),
        }
        if prior is not None:
            new_asset = media_svc.replace(
                session,
                prior,
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.video_bytes),
                width=response.width,
                height=response.height,
                duration_sec=response.duration_sec,
                metadata=metadata,
            )
        else:
            new_asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="scene_video",
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.video_bytes),
                width=response.width,
                height=response.height,
                duration_sec=response.duration_sec,
                parent_scenario_id=scenario.id,
                parent_scene_idx=render.scene_idx,
                metadata=metadata,
            )

        renders_svc.mark_video_ready(session, render, new_asset)

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            scene_idx=render.scene_idx,
            task_key="scene_video",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            video_seconds=response.duration_sec or duration_sec,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            status_="success",
        )

        renders_svc.recompute_scenario_status_from_renders(session, scenario)

        return {
            "ok": True,
            "scene_render_id": str(render.id),
            "asset_id": str(new_asset.id),
            "version": new_asset.version,
            "cost_usd": response.cost_usd,
            "duration_sec": response.duration_sec,
        }
