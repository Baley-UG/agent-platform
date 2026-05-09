"""OpenRouter LLM provider.

Implements the `LLMProvider` interface using OpenRouter's OpenAI-compatible
chat-completions endpoint. Vision inputs are sent as `image_url` parts in
the user message — both publicly fetchable URLs and base64 data URIs work.

Cost is computed from the resolved `ModelRoute.cost_per_unit_usd` snapshot;
we don't trust OpenRouter's per-call usage report for billing because the
markup model may shift, but we DO record their `usage` block for ground
truth in `generation_calls.raw`.
"""

from __future__ import annotations

import time
from typing import List, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.models.model_routes import ModelRoute
from app.services.providers.llm.base import LLMProvider, LLMResponse, VisionInput


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter returns an error or the response shape is unexpected."""


class OpenRouterProvider(LLMProvider):
    """OpenRouter chat-completions client."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self.api_key = api_key or settings.OPENROUTER_API_KEY
        self.base_url = base_url or settings.OPENROUTER_BASE_URL
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set")

    async def complete(
        self,
        prompt: str,
        route: ModelRoute,
        vision_inputs: Optional[List[VisionInput]] = None,
        system: Optional[str] = None,
    ) -> LLMResponse:
        if route.provider != "openrouter":
            raise OpenRouterError(f"OpenRouterProvider received non-openrouter route: {route.provider}")

        # Build user message content. Plain string when no vision; otherwise a list of parts.
        if vision_inputs:
            parts: list = [{"type": "text", "text": prompt}]
            for img in vision_inputs:
                if img.url:
                    parts.append({"type": "image_url", "image_url": {"url": img.url}})
                elif img.base64:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{img.mime_type};base64,{img.base64}"},
                        }
                    )
            user_content: object = parts
        else:
            user_content = prompt

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        body = {
            "model": route.model_id,
            "messages": messages,
        }
        params = route.params or {}
        for key in ("temperature", "max_tokens", "top_p", "frequency_penalty", "presence_penalty", "stop"):
            if key in params:
                body[key] = params[key]
        # Force JSON mode when the route asks for it (analyzer scenarios go through this).
        if params.get("response_format") == "json":
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if settings.OPENROUTER_HTTP_REFERER:
            headers["HTTP-Referer"] = settings.OPENROUTER_HTTP_REFERER
            headers["X-Title"] = "content_pipeline"

        url = f"{self.base_url.rstrip('/')}/chat/completions"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"openrouter http error: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            logger.warning(
                "openrouter_non_2xx",
                status=resp.status_code,
                model=route.model_id,
                body=resp.text[:1000],
            )
            raise OpenRouterError(f"openrouter {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenRouterError(f"openrouter returned non-JSON: {exc}") from exc

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"openrouter response missing choices[0].message.content: {data}") from exc

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = int(usage.get("cached_tokens") or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0)

        cost_usd = _compute_cost_usd(route, input_tokens=input_tokens, output_tokens=output_tokens)

        return LLMResponse(
            text=text or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            request_id=data.get("id"),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw={"usage": usage, "id": data.get("id"), "model": data.get("model")},
        )


def _compute_cost_usd(route: ModelRoute, *, input_tokens: int, output_tokens: int) -> float:
    """Approximate cost from the route's pricing snapshot.

    The route only carries one `cost_per_unit_usd`; for LLMs we treat
    `cost_unit='input_token'` as the per-token rate applied to TOTAL tokens
    (input + output). When admins want separate input/output pricing they
    add a second route row with priority=99 and override the analyzer to
    pick it up — out of scope for CP-M2.
    """
    if route.cost_per_unit_usd is None or route.cost_unit not in ("input_token", "output_token"):
        return 0.0
    total = (input_tokens or 0) + (output_tokens or 0)
    return float(route.cost_per_unit_usd) * total
