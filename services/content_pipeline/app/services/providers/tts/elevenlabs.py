"""ElevenLabs TTS provider.

Endpoint: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`.

Response is binary audio (mp3 by default). Cost from
`model_routes.cost_per_unit_usd` keyed on `cost_unit='input_token'`
where the unit is interpreted as **per-character** (matches ElevenLabs'
billing model). We don't get a token count back; we use len(text).
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.models.model_routes import ModelRoute
from app.services.providers.tts.base import TTSProvider, TTSResponse


class ElevenLabsError(RuntimeError):
    pass


_BASE_URL = "https://api.elevenlabs.io"


class ElevenLabsProvider(TTSProvider):
    """ElevenLabs streaming-disabled TTS client."""

    def __init__(self, *, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or settings.ELEVENLABS_API_KEY
        if not self.api_key:
            raise ElevenLabsError("ELEVENLABS_API_KEY is not set")

    async def synthesize(
        self,
        text: str,
        route: ModelRoute,
        *,
        voice_id: str,
        language: Optional[str] = None,
    ) -> TTSResponse:
        if route.provider != "elevenlabs":
            raise ElevenLabsError(f"ElevenLabsProvider received non-elevenlabs route: {route.provider}")
        if not voice_id:
            raise ElevenLabsError("voice_id is required (set on the brand_kit)")

        url = f"{_BASE_URL}/v1/text-to-speech/{voice_id}"

        params = route.params or {}
        voice_settings = {
            "stability": params.get("stability", 0.5),
            "similarity_boost": params.get("similarity_boost", 0.75),
        }
        if "style" in params:
            voice_settings["style"] = params["style"]
        if "use_speaker_boost" in params:
            voice_settings["use_speaker_boost"] = params["use_speaker_boost"]

        body: dict = {
            "text": text,
            "model_id": route.model_id,
            "voice_settings": voice_settings,
        }
        if language:
            body["language_code"] = language

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ElevenLabsError(f"elevenlabs http error: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            logger.warning(
                "elevenlabs_non_2xx", status=resp.status_code, model=route.model_id, body=resp.text[:1000]
            )
            raise ElevenLabsError(f"elevenlabs {resp.status_code}: {resp.text[:500]}")

        cost_usd = _compute_cost_usd(route, char_count=len(text))

        return TTSResponse(
            audio_bytes=resp.content,
            mime_type=resp.headers.get("content-type", "audio/mpeg"),
            request_id=resp.headers.get("request-id"),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw={"model": route.model_id, "voice_id": voice_id, "char_count": len(text)},
        )


def _compute_cost_usd(route: ModelRoute, *, char_count: int) -> float:
    """ElevenLabs charges per character; we tag the row's cost_unit as 'input_token'
    by convention (the unit is generic). Multiplier is the route's snapshot."""
    if route.cost_per_unit_usd is None or route.cost_unit not in ("input_token", "call"):
        return 0.0
    if route.cost_unit == "call":
        return float(route.cost_per_unit_usd)
    return float(route.cost_per_unit_usd) * char_count
