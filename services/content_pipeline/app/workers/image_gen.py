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

        # Phase 2 / 2.5 — director's resolved brand asset. Three sub-paths
        # depending on `image_strength`:
        #   - strength <= 0.15 → pure PASSTHROUGH (no LLM call, $0)
        #   - strength >  0.15 → IMG2IMG (Flux i2i with resolved asset
        #     as init image; the scene's image_prompt nudges it)
        #   - strength is null → fall through to legacy synth path
        # The threshold is intentionally low: <=0.15 means "the LLM
        # thinks this asset already nails the scene"; anything higher
        # we trust to img2img-remix for the matching scene-specific
        # variation the prompt describes.
        if render.resolved_asset_id is not None:
            resolved = session.get(MediaAsset, render.resolved_asset_id)
            if resolved is None:
                renders_svc.mark_failed(
                    session,
                    render,
                    f"resolved_asset_id {render.resolved_asset_id} not found",
                )
                renders_svc.recompute_scenario_status_from_renders(session, scenario)
                return {"ok": False, "error": "resolved asset missing"}
            strength = (
                float(render.image_strength) if render.image_strength is not None else None
            )
            if strength is None or strength <= 0.15:
                # Pure passthrough — image_asset_id = brand library asset.
                render.image_asset_id = resolved.id
                renders_svc.mark_image_ready(session, render)
                renders_svc.recompute_scenario_status_from_renders(session, scenario)
                calls_svc.record(
                    session,
                    project_id=scenario.project_id,
                    scenario_id=scenario.id,
                    scene_idx=render.scene_idx,
                    task_key="scene_image",
                    provider="brand_library",
                    model_id="director_pick",
                    cost_usd=0.0,
                    latency_ms=0,
                    status_="success",
                )
                logger.info(
                    "image_gen_bypassed_director_pick",
                    scene_render_id=scene_render_id,
                    asset_id=str(resolved.id),
                )
                return {
                    "ok": True,
                    "bypass": "director_pick",
                    "asset_id": str(resolved.id),
                }
            # Img2img remix branch — fall through to synth, but pass the
            # resolved asset as init. We mark a flag here and let the
            # standard synth flow run with the extra kwargs below.
            _init_s3_key = resolved.s3_key
            _init_strength = strength
        else:
            # Phase 4 — img2img-by-default. When `resolved_asset_id` is
            # NOT set, fall back to the reference frame the materializer
            # stamped onto `init_image_s3_key`. The default image_gen
            # path is img2img against the source reference; pure t2i
            # only runs when no init key exists (e.g. analyzer-less
            # admin-edited scenarios or extraction-pending reels).
            _init_s3_key = render.init_image_s3_key
            _init_strength = (
                float(render.image_strength) if render.image_strength is not None else None
            )

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

        # Phase 2.5 + Phase 4 — img2img kwargs. Two paths feed here:
        #   1. Director picked an asset (Phase 2.5) — strength came from
        #      the LLM via `scene_renders.image_strength`.
        #   2. Reference-frame default (Phase 4) — strength came from
        #      `DEFAULT_REFERENCE_STRENGTH` at materialize time.
        # In both cases `_init_s3_key` is the S3 key we presign against
        # `S3_PUBLIC_ENDPOINT` (fal must fetch from a publicly-reachable
        # host; the internal `minio:9000` is unreachable from fal).
        i2i_kwargs: dict = {}
        if _init_s3_key:
            try:
                init_url = s3.presigned_get_url(_init_s3_key, ttl=900)
                i2i_kwargs = {
                    "init_image_url": init_url,
                    # When strength is None we let the provider pick its
                    # own default. For our flow that's effectively the
                    # `DEFAULT_REFERENCE_STRENGTH` (set at materialize),
                    # so this guard is just belt-and-braces.
                    "strength": _init_strength if _init_strength is not None else 0.55,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "image_gen_i2i_presign_failed",
                    scene_render_id=scene_render_id,
                    error=str(exc),
                )

        try:
            response = asyncio.run(
                provider.generate(
                    prompt, route, width=width, height=height, **i2i_kwargs
                )
            )
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
