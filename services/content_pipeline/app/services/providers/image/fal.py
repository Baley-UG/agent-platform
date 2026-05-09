"""fal.ai image provider (Flux + family).

fal.ai exposes both a sync `/run` endpoint and an async `/queue/submit` →
`/queue/requests/{id}/status` flow. For T2I latencies (~5-30s) we use the
sync path — simpler control flow, RQ workers can wait.

Endpoint shape (`https://fal.run/<model_id>`):
  request: { prompt, image_size: "portrait_16_9" | "square" | ..., num_inference_steps, seed }
  response: { images: [{url, width, height, content_type}], timings, has_nsfw_concepts, seed }

We pass dimensions via fal's preset names (`portrait_16_9`, `square`,
`portrait_4_3`, `landscape_16_9`) when the requested aspect maps cleanly,
falling back to explicit width/height for non-standard ratios.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.models.model_routes import ModelRoute
from app.services.providers.image.base import ImageProvider, ImageResponse


class FalError(RuntimeError):
    pass


# Preferred image_size strings recognized by Flux endpoints.
_ASPECT_TO_FAL_SIZE = {
    (1080, 1920): "portrait_16_9",
    (1080, 1350): "portrait_4_3",
    (1080, 1080): "square",
    (1920, 1080): "landscape_16_9",
}


def _fal_size(width: int, height: int) -> dict:
    name = _ASPECT_TO_FAL_SIZE.get((width, height))
    if name:
        return {"image_size": name}
    return {"image_size": {"width": width, "height": height}}


class FalImageProvider(ImageProvider):
    """fal.ai client targeting any Flux model_id (e.g. `fal-ai/flux/dev`, `fal-ai/flux-pro/v1.1`)."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: str = "https://fal.run") -> None:
        self.api_key = api_key or settings.FAL_KEY
        self.base_url = base_url.rstrip("/")
        if not self.api_key:
            raise FalError("FAL_KEY is not set")

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
        if route.provider != "fal":
            raise FalError(f"FalImageProvider received non-fal route: {route.provider}")

        body: dict = {"prompt": prompt}
        body.update(_fal_size(width, height))
        params = route.params or {}
        for key in ("num_inference_steps", "guidance_scale", "num_images", "enable_safety_checker"):
            if key in params:
                body[key] = params[key]
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        if seed is not None:
            body["seed"] = seed

        url = f"{self.base_url}/{route.model_id}"
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise FalError(f"fal http error: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code >= 400:
            logger.warning("fal_non_2xx", status=resp.status_code, model=route.model_id, body=resp.text[:1000])
            raise FalError(f"fal {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise FalError(f"fal returned non-JSON: {exc}") from exc

        try:
            image = data["images"][0]
            image_url = image["url"]
            content_type = image.get("content_type", "image/png")
            out_w = image.get("width") or width
            out_h = image.get("height") or height
        except (KeyError, IndexError, TypeError) as exc:
            raise FalError(f"fal response missing images[0].url: {data}") from exc

        # fal returns a URL pointing at their CDN. Fetch the bytes here so the
        # worker can upload directly to our S3 — we never trust an external CDN
        # to outlive a job's persistence horizon.
        try:
            async with httpx.AsyncClient(timeout=settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS) as client:
                fetched = await client.get(image_url)
                fetched.raise_for_status()
        except httpx.HTTPError as exc:
            raise FalError(f"fal image fetch failed: {exc}") from exc

        cost_usd = _compute_cost_usd(route, image_count=1)

        return ImageResponse(
            image_bytes=fetched.content,
            mime_type=content_type,
            width=out_w,
            height=out_h,
            request_id=str(data.get("seed") or data.get("id") or ""),
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw={"timings": data.get("timings"), "seed": data.get("seed"), "model": route.model_id},
        )


def _compute_cost_usd(route: ModelRoute, *, image_count: int) -> float:
    if route.cost_per_unit_usd is None or route.cost_unit != "image":
        return 0.0
    return float(route.cost_per_unit_usd) * image_count
