"""Video provider interface (image-to-video).

CP-M4 ships `seedance_fal.py`. New providers (Kling, Runway, Luma, Veo)
drop in by implementing this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.models.model_routes import ModelRoute


@dataclass
class VideoResponse:
    """Normalized result from an I2V call."""

    video_bytes: bytes
    mime_type: str = "video/mp4"
    width: Optional[int] = None
    height: Optional[int] = None
    duration_sec: Optional[float] = None
    request_id: Optional[str] = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


class VideoProvider(ABC):
    """Abstract image-to-video provider."""

    @abstractmethod
    async def generate(
        self,
        *,
        image_url: str,
        prompt: str,
        route: ModelRoute,
        duration_sec: float,
        seed: Optional[int] = None,
    ) -> VideoResponse:
        """Run a single I2V call. `image_url` is a presigned S3 GET URL.

        Returns the video bytes — caller writes to S3.
        """
