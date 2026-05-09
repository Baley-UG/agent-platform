"""Image provider interface.

CP-M3 ships `fal.py` (Flux). New providers (Imagen, SDXL endpoints, local
ComfyUI) drop in by implementing this interface — no schema or worker
change required, just register the provider name in `image_gen` worker's
`_build_provider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from app.models.model_routes import ModelRoute


@dataclass
class ImageResponse:
    """Normalized result from a T2I call."""

    image_bytes: bytes
    mime_type: str = "image/png"
    width: Optional[int] = None
    height: Optional[int] = None
    request_id: Optional[str] = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


class ImageProvider(ABC):
    """Abstract T2I provider."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        route: ModelRoute,
        *,
        width: int,
        height: int,
        negative_prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> ImageResponse:
        """Run a single text-to-image call. Returns bytes — caller writes to S3."""
