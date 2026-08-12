"""audio_gen RQ task — voiceover synthesis.

Entry point: `app.workers.audio_gen.run(scenario_id, voice_id_override=None,
text_override=None)`.

Builds the voiceover script from the scenario, resolves the TTS route,
calls ElevenLabs, uploads bytes to S3, writes a versioned `media_assets`
row of type `voiceover`, links `scenario.voiceover_asset_id`, picks a
default music track if none selected yet, and transitions the scenario
to `audio_ready`.

Music selection is deliberately part of this same worker so the scenario
arrives at `audio_ready` fully audio-ready (voiceover + music both set).
A separate `reselect-music` action lets admins swap tracks without
re-running TTS.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.core import s3
from app.core.logging import logger
from app.models.brand_kits import BrandKit
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.models.scenarios import Scenario
from app.services import audio
from app.services import generation_calls as calls_svc
from app.services import media_assets as media_svc
from app.services import model_router
from app.services import scenarios as scenarios_svc
from app.services.database import session_scope
from app.services.providers.tts.base import TTSProvider
from app.services.providers.tts.elevenlabs import ElevenLabsProvider
from sqlmodel import select


def _build_provider(provider_name: str) -> TTSProvider:
    if provider_name == "elevenlabs":
        return ElevenLabsProvider()
    raise NotImplementedError(f"TTS provider not yet implemented: {provider_name}")


def _resolve_voice_id(session, scenario: Scenario, override: Optional[str]) -> Optional[str]:
    if override:
        return override
    # Prefer the project's default brand_kit voice_id.
    project = session.get(Project, scenario.project_id)
    if project and project.default_brand_kit_id:
        kit = session.get(BrandKit, project.default_brand_kit_id)
        if kit and kit.voice_id:
            return kit.voice_id
    # Fall back to any brand_kit on this project.
    kit = session.exec(
        select(BrandKit).where(BrandKit.project_id == scenario.project_id).order_by(BrandKit.is_default.desc())
    ).first()
    if kit and kit.voice_id:
        return kit.voice_id
    return None


def run(
    scenario_id: str,
    voice_id_override: Optional[str] = None,
    text_override: Optional[str] = None,
) -> dict:
    scenario_uuid = uuid.UUID(scenario_id)

    with session_scope() as session:
        scenario = session.get(Scenario, scenario_uuid)
        if scenario is None:
            logger.warning("audio_gen_scenario_missing", scenario_id=scenario_id)
            return {"ok": False, "error": "scenario not found"}

        voice_id = _resolve_voice_id(session, scenario, voice_id_override)
        if not voice_id:
            scenarios_svc.mark_failed(
                session, scenario, "no voice_id resolved (set on a brand_kit or pass voice_id_override)"
            )
            return {"ok": False, "error": "no voice_id"}

        try:
            route = model_router.resolve(session, "voiceover_tts", project_id=scenario.project_id)
        except model_router.NoRouteError as exc:
            scenarios_svc.mark_failed(session, scenario, f"no TTS route: {exc}")
            return {"ok": False, "error": str(exc)}

        provider = _build_provider(route.provider)

        # Two synthesis modes:
        #   * SCENE-ALIGNED (default): one TTS clip per narrated scene.
        #     Compose delays each clip to its scene's start so speech
        #     lands exactly on its scene. Clips are media_assets rows
        #     with type='voiceover_scene' + parent_scene_idx.
        #   * LEGACY single-file: forced by `text_override` (admin gave
        #     one continuous script — per-scene split would be wrong).
        scene_lines = [] if text_override else audio.scene_voiceover_texts(scenario)

        if scene_lines:
            return _run_per_scene(
                session, scenario, provider, route, voice_id, scene_lines
            )

        # ---- Legacy single-file path ----
        text = (text_override or audio.build_voiceover_script(scenario)).strip()
        if not text:
            scenarios_svc.mark_failed(session, scenario, "voiceover script is empty")
            return {"ok": False, "error": "empty script"}

        try:
            response = asyncio.run(
                provider.synthesize(text, route, voice_id=voice_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("audio_gen_call_failed", scenario_id=scenario_id, error=str(exc))
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                task_key="voiceover_tts",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            scenarios_svc.mark_failed(session, scenario, str(exc))
            return {"ok": False, "error": str(exc)}

        key = s3.make_key(
            scenario.project_id,
            "audio",
            audio.voiceover_filename(scenario.id, scenario.version),
        )
        s3.upload_bytes(key, response.audio_bytes, content_type=response.mime_type)

        prior_voice = (
            session.get(MediaAsset, scenario.voiceover_asset_id)
            if scenario.voiceover_asset_id
            else None
        )
        metadata = {
            "model_id": route.model_id,
            "voice_id": voice_id,
            "char_count": len(text),
            "provider": route.provider,
            "raw": response.raw,
        }
        if prior_voice is not None:
            new_asset = media_svc.replace(
                session,
                prior_voice,
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.audio_bytes),
                duration_sec=response.duration_sec,
                metadata=metadata,
            )
        else:
            new_asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="voiceover",
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.audio_bytes),
                duration_sec=response.duration_sec,
                parent_scenario_id=scenario.id,
                metadata=metadata,
            )

        scenario.voiceover_asset_id = new_asset.id
        _pick_music_and_finish(session, scenario)

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            task_key="voiceover_tts",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            audio_seconds=response.duration_sec,
            unit_count=len(text),
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            status_="success",
        )

        scenarios_svc.mark_audio_ready(session, scenario)

        return {
            "ok": True,
            "scenario_id": str(scenario.id),
            "voiceover_asset_id": str(new_asset.id),
            "music_track_id": str(scenario.music_track_id) if scenario.music_track_id else None,
            "version": new_asset.version,
            "cost_usd": response.cost_usd,
            "char_count": len(text),
        }


def _pick_music_and_finish(session, scenario: Scenario) -> None:
    """Auto-pick a music track if none chosen yet, persist the scenario."""
    if scenario.music_track_id is None:
        track = audio.select_music_for_scenario(session, scenario)
        if track is not None:
            scenario.music_track_id = track.id
    session.add(scenario)
    session.flush()


def _run_per_scene(
    session,
    scenario: Scenario,
    provider: TTSProvider,
    route,
    voice_id: str,
    scene_lines: list[tuple[int, str]],
) -> dict:
    """Scene-aligned TTS: one clip per narrated scene.

    Prior clips for this scenario version get superseded via the
    media_assets replace-chain keyed on `(parent_scenario_id,
    parent_scene_idx, type='voiceover_scene')`. Any single scene's TTS
    failure fails the whole run (conservative — matching image_gen's
    rollup rule).

    `scenario.voiceover_asset_id` is pointed at the FIRST clip so
    downstream "has voiceover" checks (pipeline actions, /progress
    voiceover block) stay truthy without schema changes.
    """
    total_cost = 0.0
    total_chars = 0
    clip_assets: list[MediaAsset] = []

    # Prior active clips per scene_pos, for versioned replacement.
    prior_by_pos: dict[int, MediaAsset] = {}
    for row in session.exec(
        select(MediaAsset).where(
            MediaAsset.parent_scenario_id == scenario.id,
            MediaAsset.type == "voiceover_scene",
            MediaAsset.replaced_by_id.is_(None),
        )
    ).all():
        if row.parent_scene_idx is not None:
            prior_by_pos[row.parent_scene_idx] = row

    for scene_pos, text in scene_lines:
        try:
            response = asyncio.run(provider.synthesize(text, route, voice_id=voice_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "audio_gen_scene_call_failed",
                scenario_id=str(scenario.id),
                scene_pos=scene_pos,
                error=str(exc),
            )
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                task_key="voiceover_tts",
                provider=route.provider,
                model_id=route.model_id,
                status_="failed",
                error=str(exc)[:1000],
            )
            scenarios_svc.mark_failed(session, scenario, f"scene {scene_pos} TTS: {exc}")
            return {"ok": False, "error": str(exc), "scene_pos": scene_pos}

        key = s3.make_key(
            scenario.project_id,
            "audio",
            audio.scene_voiceover_filename(scenario.id, scenario.version, scene_pos),
        )
        s3.upload_bytes(key, response.audio_bytes, content_type=response.mime_type)

        metadata = {
            "model_id": route.model_id,
            "voice_id": voice_id,
            "char_count": len(text),
            "provider": route.provider,
            "scene_pos": scene_pos,
        }
        prior = prior_by_pos.get(scene_pos)
        if prior is not None:
            asset = media_svc.replace(
                session,
                prior,
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.audio_bytes),
                duration_sec=response.duration_sec,
                metadata=metadata,
            )
        else:
            asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="voiceover_scene",
                s3_key=key,
                mime_type=response.mime_type,
                size_bytes=len(response.audio_bytes),
                duration_sec=response.duration_sec,
                parent_scenario_id=scenario.id,
                parent_scene_idx=scene_pos,
                metadata=metadata,
            )
        clip_assets.append(asset)

        total_cost += response.cost_usd or 0.0
        total_chars += len(text)
        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            task_key="voiceover_tts",
            provider=route.provider,
            model_id=route.model_id,
            request_id=response.request_id,
            audio_seconds=response.duration_sec,
            unit_count=len(text),
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            status_="success",
        )

    # Keep legacy pointers truthy for downstream checks.
    scenario.voiceover_asset_id = clip_assets[0].id if clip_assets else None
    _pick_music_and_finish(session, scenario)
    scenarios_svc.mark_audio_ready(session, scenario)

    logger.info(
        "audio_gen_per_scene_done",
        scenario_id=str(scenario.id),
        clips=len(clip_assets),
        cost_usd=total_cost,
    )
    return {
        "ok": True,
        "scenario_id": str(scenario.id),
        "mode": "per_scene",
        "clips": len(clip_assets),
        "music_track_id": str(scenario.music_track_id) if scenario.music_track_id else None,
        "cost_usd": total_cost,
        "char_count": total_chars,
    }
