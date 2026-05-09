"""LLM provider interface.

Implementations live in `openrouter.py` (CP-M2), `anthropic_direct.py`
(later), etc. The interface is intentionally narrow: take a prompt and a
resolved `ModelRoute`, return text + usage. Vision inputs are passed as
a list of base64-or-URL image references.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from app.models.model_routes import ModelRoute


@dataclass
class VisionInput:
    """A single image attached to an LLM prompt."""

    url: Optional[str] = None  # publicly fetchable URL OR data: URL
    base64: Optional[str] = None  # raw base64 (no data: prefix)
    mime_type: str = "image/jpeg"


@dataclass
class LLMResponse:
    """Normalized result from a provider call."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    request_id: Optional[str] = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    raw: dict = field(default_factory=dict)


class LLMProvider(ABC):
    """Abstract LLM provider — concrete impls land in CP-M2."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        route: ModelRoute,
        vision_inputs: Optional[List[VisionInput]] = None,
        system: Optional[str] = None,
    ) -> LLMResponse:
        """Run a single completion. Implementations record latency & cost in the response."""
