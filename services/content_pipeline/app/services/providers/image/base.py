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
    """Abstract image provider — handles both T2I and I2I.

    Concrete providers may switch model endpoint internally based on
    whether `init_image_url` is set (Flux dev → `image-to-image`,
    SDXL → `img2img`, etc.). Providers that don't support I2I should
    raise rather than silently fall back to T2I — the caller wants the
    semantic guarantee that strength was applied.
    """

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
        init_image_url: Optional[str] = None,
        strength: Optional[float] = None,
    ) -> ImageResponse:
        """Single image generation call. Returns raw bytes; caller writes to S3.

        - `init_image_url`: when set, run image-to-image (provider hits
          the i2i variant of the route's model). When None, pure T2I.
        - `strength`: 0..1 remix amount. Only meaningful for I2I.
        """
