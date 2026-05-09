"""TTS provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.models.model_routes import ModelRoute


@dataclass
class TTSResponse:
    """Normalized result from a TTS call."""

    audio_bytes: bytes
    mime_type: str = "audio/mpeg"
    duration_sec: Optional[float] = None
    request_id: Optional[str] = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


class TTSProvider(ABC):
    """Abstract text-to-speech provider."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        route: ModelRoute,
        *,
        voice_id: str,
        language: Optional[str] = None,
    ) -> TTSResponse:
        """Render `text` to audio bytes via the configured voice."""
