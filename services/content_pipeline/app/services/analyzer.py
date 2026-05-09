"""Analyzer — turn a `content_reference` into a structured `scenario_json`.

Flow:
1. Build the analyzer prompt from caption + transcript + provider metadata.
2. (Optional, CP-M3+) Pull keyframes from the reference media into S3 and
   attach them as `VisionInput`s. CP-M2 ships caption/transcript-only.
3. Resolve the LLM route via `model_router` (`task_key='scenario_analysis'`).
4. Call OpenRouter (or whatever provider the route resolves to).
5. Parse JSON response → validate scene shape → return.

The worker layer (`app.workers.analyzer`) wraps this with state transitions
and ledger writes; tests can call `analyze_reference()` directly with a
fake provider.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Optional

from app.core.logging import logger
from app.models.content_references import ContentReference
from app.models.model_routes import ModelRoute
from app.services.providers.llm.base import LLMProvider, LLMResponse, VisionInput


SYSTEM_PROMPT = """You are a senior creative director who reverse-engineers short-form social
videos into reusable production scripts (scenarios).

Given a reference piece of content, you produce ORIGINAL scenarios in the SAME genre / mood —
never copy the source material verbatim. Avoid trademarks, named persons, and direct quotes
from the reference.

Return ONLY a single JSON object matching this exact schema (no markdown, no commentary):

{
  "duration_sec": number,                   // total target duration, 8-90
  "hook": string,                           // first 1-2s gripper
  "cta": string,                            // closing call-to-action (or empty)
  "music": {"mood": string, "bpm_range": [number, number]},
  "outro_template_id": string | null,
  "scenes": [
    {
      "idx": number,
      "duration": number,                   // seconds, 1.5-10
      "image_prompt": string,               // 9:16-friendly T2I prompt
      "motion_prompt": string,              // I2V camera + subject motion
      "on_screen_text": string,             // empty when none
      "transition_out": string,             // 'cut' | 'whip_pan_left' | 'fade' | etc.
      "voiceover": string,                  // empty when scene is silent
      "audio_mood": string                  // tag matching music_tracks.mood
    }
  ]
}

Rules:
- Scenes must cover the full duration_sec (sum of scene.duration ≈ duration_sec, within ±0.5s).
- 3-7 scenes total.
- image_prompt must work as a standalone T2I prompt (subject + setting + lighting + style).
- Output strictly valid JSON — no trailing commas, no comments.
"""


def build_user_prompt(reference: ContentReference, brand_style_suffix: Optional[str] = None) -> str:
    """Assemble the analyzer's user message from reference fields."""
    meta = reference.metadata_json or {}
    parts = [
        "Reference content brief:",
        f"- source: {reference.source_provider}",
    ]
    if reference.source_url:
        parts.append(f"- url: {reference.source_url}")
    if reference.caption:
        parts.append(f"- caption:\n{reference.caption.strip()}")
    if reference.transcript:
        parts.append(f"- transcript:\n{reference.transcript.strip()}")
    for key in ("media_type", "duration_sec", "play_count", "like_count", "score"):
        value = meta.get(key)
        if value is not None:
            parts.append(f"- {key}: {value}")
    if reference.hashtags:
        parts.append("- hashtags: " + " ".join(reference.hashtags))
    if brand_style_suffix:
        parts.append(f"\nBrand style direction: {brand_style_suffix}")
    parts.append(
        "\nProduce a scenario JSON for a 9:16 short-form video in the SAME genre/mood. "
        "Originality is mandatory — do NOT recreate specific frames or quotes from the reference."
    )
    return "\n".join(parts)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_scenario_json(text: str) -> dict:
    """Extract and parse the analyzer's JSON output, tolerating fenced output."""
    cleaned = text.strip()
    # Strip ```json fences if the model added them despite our instructions.
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
        cleaned = cleaned.rstrip("`").strip()
    # Last-resort: find the outermost {...} block.
    if not cleaned.startswith("{"):
        match = _JSON_BLOCK_RE.search(cleaned)
        if not match:
            raise ValueError(f"analyzer response does not contain JSON object: {text[:500]}")
        cleaned = match.group(0)
    return json.loads(cleaned)


def validate_scenario(payload: dict) -> dict:
    """Lightweight shape validation. Returns the (possibly normalized) payload.

    Strict pydantic validation lives on the Scenario edit endpoint; here we
    just guarantee the analyzer didn't return garbage.
    """
    if not isinstance(payload, dict):
        raise ValueError("scenario JSON is not an object")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("scenario.scenes must be a non-empty array")
    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"scenes[{i}] is not an object")
        for field in ("idx", "duration", "image_prompt", "motion_prompt"):
            if field not in scene:
                raise ValueError(f"scenes[{i}] missing required field: {field}")
    return payload


async def analyze_reference(
    *,
    reference: ContentReference,
    route: ModelRoute,
    provider: LLMProvider,
    brand_style_suffix: Optional[str] = None,
    vision_inputs: Optional[list[VisionInput]] = None,
) -> tuple[dict, LLMResponse]:
    """Run the analyzer end-to-end. Returns (scenario_json, llm_response).

    Caller is responsible for writing the `generation_calls` row and updating
    the scenario row — keeps this function easy to unit test with fakes.
    """
    user_prompt = build_user_prompt(reference, brand_style_suffix=brand_style_suffix)
    response = await provider.complete(
        prompt=user_prompt,
        route=route,
        vision_inputs=vision_inputs,
        system=SYSTEM_PROMPT,
    )
    try:
        scenario = parse_scenario_json(response.text)
        scenario = validate_scenario(scenario)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("analyzer_parse_failed", error=str(exc), preview=response.text[:300])
        raise
    return scenario, response
