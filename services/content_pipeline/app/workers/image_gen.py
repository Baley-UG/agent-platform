"""image_gen RQ task.

Entry point: `app.workers.image_gen.run(scene_render_id, prompt_override=None)`.

Resolves the T2I route, picks the concrete provider, generates one image
sized to the scene_render's aspect, uploads bytes to S3, and creates a
`media_assets` row (initial or replacement). Records `generation_calls`
either way and rolls up `scenario.status`.
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
from app.services import presets
from app.services import scene_renders as renders_svc
from app.services.database import session_scope
from app.services.providers.image.base import ImageProvider
from app.services.providers.image.fal import FalImageProvider


def _build_provider(provider_name: str) -> ImageProvider:
    if provider_name == "fal":
        return FalImageProvider()
    raise NotImplementedError(f"image provider not yet implemented: {provider_name}")


def _scene_for_idx(scenario: Scenario, scene_idx: int) -> Optional[dict]:
    if not scenario.scenario_json:
        return None
    for scene in scenario.scenario_json.get("scenes") or []:
        if scene.get("idx") == scene_idx:
            return scene
    return None


def _image_prompt(scene: dict, brand_style_suffix: Optional[str]) -> str:
    base = (scene.get("image_prompt") or "").strip()
    if brand_style_suffix:
        base = f"{base}\nStyle: {brand_style_suffix.strip()}"
    return base


def run(scene_render_id: str, prompt_override: Optional[str] = None) -> dict:
    """RQ entry point. Returns a small dict summary."""
    render_uuid = uuid.UUID(scene_render_id)

    with session_scope() as session:
        render = session.get(SceneRender, render_uuid)
        if render is None:
            logger.warning("image_gen_render_missing", scene_render_id=scene_render_id)
            return {"ok": False, "error": "scene_render not found"}

        scenario = session.get(Scenario, render.scenario_id)
        if scenario is None or scenario.scenario_json is None:
            renders_svc.mark_failed(session, render, "scenario or scenario_json missing")
            return {"ok": False, "error": "scenario missing"}

        scene = _scene_for_idx(scenario, render.scene_idx)
        if scene is None:
            renders_svc.mark_failed(session, render, f"scene_idx {render.scene_idx} not found in scenario_json")
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": "scene_idx not in scenario_json"}

        prompt = prompt_override or _image_prompt(scene, brand_style_suffix=None)
        if not prompt:
            renders_svc.mark_failed(session, render, "scene.image_prompt is empty")
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": "empty prompt"}

        try:
            width, height = presets.aspect_dimensions(render.aspect_ratio)
        except KeyError:
            renders_svc.mark_failed(session, render, f"unknown aspect_ratio: {render.aspect_ratio}")
            return {"ok": False, "error": "unknown aspect"}

        try:
            route = model_router.resolve(session, "scene_image", project_id=scenario.project_id)
        except model_router.NoRouteError as exc:
            renders_svc.mark_failed(session, render, f"no T2I route: {exc}")
            return {"ok": False, "error": str(exc)}

        renders_svc.mark_generating_image(session, render)

        provider = _build_provider(route.provider)

        try:
            response = asyncio.run(provider.generate(prompt, route, width=width, height=height))
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_gen_call_failed", scene_render_id=scene_render_id, error=str(exc))
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                scene_idx=render.scene_idx,
                task_key="scene_image",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            renders_svc.mark_failed(session, render, str(exc))
            renders_svc.recompute_scenario_status_from_renders(session, scenario)
            return {"ok": False, "error": str(exc)}

        # Upload bytes to S3.
        ext = "png" if response.mime_type.endswith("png") else "jpg"
        key = s3.make_key(
            scenario.project_id,
            "scenes",
            f"scenario-{scenario.id}-scene-{render.scene_idx}-{render.aspect_ratio.replace(':', 'x')}.{ext}",
        )
        s3.upload_bytes(key, response.image_bytes, content_type=response.mime_type)

        # Versioned media_assets write.
        prior_asset_id = render.image_asset_id
        prior: Optional[MediaAsset] = session.get(MediaAsset, prior_asset_id) if prior_asset_id else None
        metadata = {
            "aspect_ratio": render.aspect_ratio,
            "model_id": route.model_id,
            "provider": route.provider,
            "raw": response.raw,
            "prompt": prompt,
        }
        if prior is not None:
            new_asset = media_svc.replace(
                session,
                prior,
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.image_bytes),
                width=response.width,
                height=response.height,
                metadata=metadata,
            )
        else:
            new_asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="scene_image",
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.image_bytes),
                width=response.width,
                height=response.height,
                parent_scenario_id=scenario.id,
                parent_scene_idx=render.scene_idx,
                metadata=metadata,
            )

        renders_svc.mark_image_ready(session, render, new_asset)

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            scene_idx=render.scene_idx,
            task_key="scene_image",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            image_count=1,
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
        }
