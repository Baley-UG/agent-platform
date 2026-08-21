"""Generic fal.ai async-queue client for the remake vertical.

Every fal task the remake pipeline uses — v2v restyle, video inpaint,
i2v, keyframe edit, whisper, upscale — hits the same queue protocol:

  POST https://queue.fal.run/<model_id>   → {request_id, status_url, response_url}
  GET  <status_url>                        → {status: IN_QUEUE|IN_PROGRESS|COMPLETED|FAILED}
  GET  <response_url>                       → final output JSON

This client generalizes `seedance_fal.py`'s submit/poll/backoff loop:
the request body comes from a per-task_key param-mapper, and the output
URL is extracted by walking a few well-known shapes ({video:{url}},
{image:{url}}, {images:[{url}]}, {url}). It returns the final JSON plus
the primary output URL; callers download bytes themselves.

Fallback chains are REAL here (unlike v1's dead `resolve_chain`): on a
submit 4xx/5xx or terminal FAILED, `run_task` walks to the next enabled
route for the task_key and records each failed attempt on the ledger.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logging import logger
from app.models.model_routes import ModelRoute

_QUEUE_BASE = "https://queue.fal.run"
_INITIAL_POLL_SECONDS = 4.0
_MAX_POLL_SECONDS = 30.0


class FalQueueError(RuntimeError):
    pass


@dataclass
class FalResult:
    output_url: Optional[str]
    result: dict
    request_id: Optional[str]
    latency_ms: int
    model_id: str
    # Extra output URLs (e.g. every image of a multi-image edit).
    output_urls: List[str] = field(default_factory=list)


def _extract_output_url(result: dict) -> tuple[Optional[str], List[str]]:
    """Walk fal's common output shapes for the primary media URL."""
    urls: List[str] = []
    for key in ("video", "image", "audio"):
        block = result.get(key)
        if isinstance(block, dict) and block.get("url"):
            urls.append(block["url"])
    for key in ("images", "videos", "frames"):
        block = result.get(key)
        if isinstance(block, list):
            urls.extend(b["url"] for b in block if isinstance(b, dict) and b.get("url"))
    if result.get("url"):
        urls.append(result["url"])
    return (urls[0] if urls else None), urls


def _api_key() -> str:
    key = settings.FAL_KEY
    if not key:
        raise FalQueueError("FAL_KEY is not set")
    return key


async def submit_and_poll(model_id: str, body: dict, *, timeout_seconds: float) -> FalResult:
    """One submit → poll → fetch cycle against a single model. Raises
    FalQueueError on any failure (so `run_task` can try the fallback)."""
    headers = {"Authorization": f"Key {_api_key()}", "Content-Type": "application/json"}
    submit_url = f"{_QUEUE_BASE}/{model_id}"
    http_timeout = httpx.Timeout(settings.CP_ANALYZER_HTTP_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout_seconds
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=http_timeout) as client:
        try:
            resp = await client.post(submit_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise FalQueueError(f"fal submit http error ({model_id}): {exc}") from exc
        if resp.status_code >= 400:
            raise FalQueueError(f"fal submit {resp.status_code} ({model_id}): {resp.text[:400]}")
        try:
            submit = resp.json()
            status_url = submit["status_url"]
            response_url = submit["response_url"]
            request_id = submit.get("request_id")
        except (ValueError, KeyError) as exc:
            raise FalQueueError(f"fal submit missing fields ({model_id}): {resp.text[:300]}") from exc

        poll_interval = _INITIAL_POLL_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise FalQueueError(f"fal poll timed out ({model_id}, request_id={request_id})")
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, _MAX_POLL_SECONDS)
            try:
                poll = await client.get(status_url, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("fal_poll_transient", model=model_id, error=str(exc))
                continue
            if poll.status_code >= 500:
                continue
            if poll.status_code >= 400:
                raise FalQueueError(f"fal poll {poll.status_code} ({model_id}): {poll.text[:300]}")
            try:
                state = (poll.json().get("status") or "").upper()
            except ValueError:
                continue
            if state in ("COMPLETED", "OK", "SUCCEEDED"):
                break
            if state in ("FAILED", "ERROR", "CANCELLED"):
                raise FalQueueError(f"fal task {state} ({model_id}, request_id={request_id})")

        try:
            resp_get = await client.get(response_url, headers=headers)
            resp_get.raise_for_status()
            result = resp_get.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FalQueueError(f"fal response fetch failed ({model_id}): {exc}") from exc

    primary, urls = _extract_output_url(result)
    return FalResult(
        output_url=primary,
        output_urls=urls,
        result=result,
        request_id=request_id,
        latency_ms=int((time.monotonic() - started) * 1000),
        model_id=model_id,
    )


def run_task(
    routes: List[ModelRoute],
    body_for: Callable[[ModelRoute], Dict[str, Any]],
    *,
    timeout_seconds: float,
    on_attempt_failed: Optional[Callable[[ModelRoute, Exception], None]] = None,
) -> FalResult:
    """Try each route in priority order until one succeeds (real fallback).

    `routes` is `model_router.resolve_chain(...)`. `body_for(route)` maps
    the caller's inputs into the request body for that specific model.
    `on_attempt_failed` lets the caller record a failed ledger row per
    attempt. Runs the async cycle via `asyncio.run` (RQ workers are sync).
    """
    if not routes:
        raise FalQueueError("no routes provided")
    last_exc: Optional[Exception] = None
    for route in routes:
        try:
            return asyncio.run(
                submit_and_poll(route.model_id, body_for(route), timeout_seconds=timeout_seconds)
            )
        except Exception as exc:  # noqa: BLE001 — try the next route
            last_exc = exc
            logger.warning("fal_task_attempt_failed", model=route.model_id, error=str(exc))
            if on_attempt_failed is not None:
                try:
                    on_attempt_failed(route, exc)
                except Exception:  # noqa: BLE001
                    pass
    raise FalQueueError(f"all fal routes failed: {last_exc}")
