"""Seedance image-to-video via fal.ai's async queue.

fal.ai hosts ByteDance's Seedance models alongside Flux. Their queue API:

  POST https://queue.fal.run/<model_id>
    body: {image_url, prompt, duration, ...}
    → 202, { request_id, status_url, response_url, cancel_url }

  GET <status_url>
    → { status: "IN_QUEUE" | "IN_PROGRESS" | "COMPLETED" | "FAILED", queue_position, logs }

  GET <response_url>
    → final model output, including {video: {url, content_type, file_size}}

Seedance model_ids (set in `model_routes.model_id`):
  fal-ai/bytedance/seedance/v1/pro/image-to-video
  fal-ai/bytedance/seedance/v1/lite/image-to-video

We poll with backoff (5s → 30s capped) and a hard wall-clock cap drawn
from `CP_VIDEO_GEN_TIMEOUT_SECONDS`. Cost computed from
`model_routes.cost_per_unit_usd` keyed on `cost_unit='video_second'`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.models.model_routes import ModelRoute
from app.services.providers.video.base import VideoProvider, VideoResponse


class SeedanceError(RuntimeError):
    pass


_QUEUE_BASE = "https://queue.fal.run"
_INITIAL_POLL_SECONDS = 5.0
_MAX_POLL_SECONDS = 30.0


class SeedanceFalProvider(VideoProvider):
    """Seedance I2V routed through fal.ai's async queue."""

    def __init__(self, *, api_key: Optional[str] = None) -> None:
        # Seedance via fal.ai uses the same FAL_KEY as the image provider.
        # If the user later moves to a direct Volcano/ByteDance endpoint,
        # add a SEEDANCE_API_KEY check here.
        self.api_key = api_key or settings.SEEDANCE_API_KEY or settings.FAL_KEY
        if not self.api_key:
            raise SeedanceError("neither SEEDANCE_API_KEY nor FAL_KEY is set")

    async def generate(
        self,
        *,
        image_url: str,
        prompt: str,
        route: ModelRoute,
        duration_sec: float,
        seed: Optional[int] = None,
    ) -> VideoResponse:
        if route.provider != "seedance":
            raise SeedanceError(f"SeedanceFalProvider received non-seedance route: {route.provider}")

        body: dict = {
            "image_url": image_url,
            "prompt": prompt,
            # Seedance accepts integer seconds; round up so a 5.4s scene
            # still gets at least 5s of motion.
            "duration": max(1, int(round(duration_sec))),
        }
        params = route.params or {}
        for key in ("aspect_ratio", "resolution", "num_inference_steps"):
            if key in params:
                body[key] = params[key]
        if seed is not None:
            body["seed"] = seed

        submit_url = f"{_QUEUE_BASE}/{route.model_id}"
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = httpx.Timeout(settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS)
        deadline = time.monotonic() + settings.CP_VIDEO_GEN_TIMEOUT_SECONDS
        started = time.monotonic()

        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(submit_url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise SeedanceError(f"seedance submit http error: {exc}") from exc
            if resp.status_code >= 400:
                logger.warning("seedance_submit_non_2xx", status=resp.status_code, body=resp.text[:1000])
                raise SeedanceError(f"seedance submit {resp.status_code}: {resp.text[:500]}")
            try:
                submit = resp.json()
                status_url = submit["status_url"]
                response_url = submit["response_url"]
                request_id = submit.get("request_id")
            except (ValueError, KeyError) as exc:
                raise SeedanceError(f"seedance submit response missing fields: {resp.text[:500]}") from exc

            # Poll status with backoff.
            poll_interval = _INITIAL_POLL_SECONDS
            final_status: dict = {}
            while True:
                if time.monotonic() > deadline:
                    raise SeedanceError(
                        f"seedance poll timed out after {settings.CP_VIDEO_GEN_TIMEOUT_SECONDS}s "
                        f"(request_id={request_id})"
                    )
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, _MAX_POLL_SECONDS)
                try:
                    poll = await client.get(status_url, headers=headers)
                except httpx.HTTPError as exc:
                    logger.warning("seedance_poll_transient", error=str(exc), request_id=request_id)
                    continue
                if poll.status_code >= 500:
                    logger.warning("seedance_poll_5xx", status=poll.status_code, request_id=request_id)
                    continue
                if poll.status_code >= 400:
                    raise SeedanceError(f"seedance poll {poll.status_code}: {poll.text[:500]}")
                try:
                    poll_data = poll.json()
                except ValueError:
                    continue
                state = (poll_data.get("status") or "").upper()
                if state in ("COMPLETED", "OK", "SUCCEEDED"):
                    final_status = poll_data
                    break
                if state in ("FAILED", "ERROR", "CANCELLED"):
                    raise SeedanceError(
                        f"seedance task failed (request_id={request_id}): {poll_data}"
                    )

            # Fetch the final response.
            try:
                resp_get = await client.get(response_url, headers=headers)
                resp_get.raise_for_status()
                result = resp_get.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SeedanceError(f"seedance response fetch failed: {exc}") from exc

            video_block = result.get("video") or {}
            video_url = video_block.get("url")
            if not video_url:
                raise SeedanceError(f"seedance response missing video.url: {result}")

            try:
                video_resp = await client.get(video_url)
                video_resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise SeedanceError(f"seedance video fetch failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        cost_usd = _compute_cost_usd(route, duration_sec=duration_sec)

        return VideoResponse(
            video_bytes=video_resp.content,
            mime_type=video_block.get("content_type") or "video/mp4",
            duration_sec=float(body["duration"]),
            request_id=request_id,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            raw={"final": final_status, "model": route.model_id, "result": result},
        )


def _compute_cost_usd(route: ModelRoute, *, duration_sec: float) -> float:
    if route.cost_per_unit_usd is None or route.cost_unit != "video_second":
        return 0.0
    return float(route.cost_per_unit_usd) * float(duration_sec)
