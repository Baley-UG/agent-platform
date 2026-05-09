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

        # Build script (or use override).
        text = (text_override or audio.build_voiceover_script(scenario)).strip()
        if not text:
            scenarios_svc.mark_failed(session, scenario, "voiceover script is empty")
            return {"ok": False, "error": "empty script"}

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

        # Upload bytes.
        ext = "mp3" if response.mime_type.startswith("audio/mpeg") else "audio"
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

        # Auto-pick a music track if none chosen yet.
        if scenario.music_track_id is None:
            track = audio.select_music_for_scenario(session, scenario)
            if track is not None:
                scenario.music_track_id = track.id

        session.add(scenario)
        session.flush()

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
