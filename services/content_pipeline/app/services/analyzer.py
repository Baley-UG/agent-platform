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


SYSTEM_PROMPT = """You are a senior short-form content director. You are given a reference
post — usually ONE or MORE attached images plus its caption and metadata. Your job is to
produce a production script (a "scenario") for the platform's content team.

# 0. GROUND YOUR SCENARIO IN WHAT YOU ACTUALLY SEE

The attached image(s) are the SOURCE OF TRUTH. The caption is supporting context only.

## IMPORTANT — the image generator runs IMG2IMG, not text-to-image

Downstream, each scene's `image_prompt` is fed to Flux's **image-to-image**
endpoint with the matching source frame as the INIT IMAGE. The model
already has the composition, lighting, palette, subject pose, era, props
from the init frame — your prompt's job is to describe **what should
CHANGE**, not to re-describe the whole scene.

## How to write `image_prompt`

  * Lead with the kept-from-source nouns ("same girl, same living room,
    same warm window light") so the model stays anchored.
  * Then list THE DELTAS — only the elements that should look different.
    Examples:
      - "...lift the hand higher into frame, glass tilted toward camera"
      - "...recolor the wall behind her to cream, keep the warm rim light"
      - "...replace the brand-name cereal box with a generic muesli pouch"
  * Keep it short. 1-3 sentences max. Img2img is sensitive to long
    prompts — the more you write, the harder it remixes away from the
    init frame.
  * Do NOT describe a from-scratch composition. The model already SEES
    the frame; writing "cinematic close-up of a hand pouring coffee
    onto..." overrides what's there and produces hallucinated drift.
  * Do NOT include camera lens jargon ("85mm", "f/1.4") unless the source
    already has that look you want to keep. Generic style tags
    ("cinematic", "documentary") are fine when the source matches them.
  * The ONLY allowed substitutions (for copyright / brand safety):
      - Specific person → same age/role/activity, different face/outfit.
      - Brand-name product → generic version of the same category.
      - On-screen text → different exact words; same length / position /
        message intent. (Source "Save for later" → "Bookmark this" OK;
        "Comment below" NOT — function changed.)
  * Do NOT add elements the source image doesn't contain.
  * Do NOT remove elements the source image clearly contains.

## How to write `motion_prompt` (for the video stage)

  * Same delta principle, but for motion. "slow push-in, hold the hand
    raise". NOT a full new shot description.
  * "static" is a valid value when the scene shouldn't move.

## Scene count vs slides

  * `source kind=photo` → exactly 1 scene that re-stages the single source frame.
  * `source kind=carousel` → exactly as many scenes as the number of slides attached
    (one scene per slide; each scene mirrors its slide's visual content).
  * `source kind=reel` / `video` → 3-7 scenes that pace through the source's action.

Only invent fully new visual content when the source is too sparse to fill the runtime.
In that case mark the invented frame with `motion_prompt: "filler"` so the producer
can flag it for revision.

# 1. HOW TO ADAPT TO SOURCE KIND

Every brief contains a `source kind` token. The output schema below is fixed, but the values
you put inside it differ:

- `photo` (single still image)
  * Exactly ONE scene. `duration_sec` 4-8 (feed pacing, not reel).
  * `motion_prompt` MINIMAL — subtle parallax / slow zoom / "static composition".
  * `transition_out` = "fade". No on-screen text unless the reference clearly has it.

- `carousel` (multi-image slideshow)
  * One scene PER VISIBLE SLIDE in the attached images. If you receive 3
    image attachments, produce EXACTLY 3 scenes — do not invent extras.
    If you receive 1 image, produce 1 scene; the missing slides are
    unavailable to you and must not be fabricated.
  * Each scene 2-4s. `duration_sec` ≈ scene_count × 3.
  * `motion_prompt` minimal (slow pan / push-in), NOT video motion.
  * `transition_out` = "fade" or "slide".

- `reel` / `video`
  * 3-7 scenes, 1.5-10s each, `duration_sec` 8-90.
  * Rich `motion_prompt` (camera move + subject action).
  * First scene <= 2s — the hook owns the opening.

# 2. HOOK CRAFT (the first 1-2 seconds)

A great hook does ONE of these — pick the one that fits the reference:
  - Pattern interrupt: unexpected visual / contradictory statement
  - Curiosity gap: question or claim whose answer is the rest of the video
  - Bold promise: "this changed X overnight" / "everyone is using Y wrong"
  - Relatable callout: "if you ever feel Z, watch this"
  - Visual mystery: a striking image whose context is revealed later

NEVER start with a logo, a slow build-up, or "hi guys". The first frame must earn the second.

# 3. PACING & ATTENTION

- Drop a NEW visual or beat every 1.5-3 seconds (reel/video) or 2-4 seconds (carousel).
- Reserve the longest single scene for the payoff (revelation, transformation, result).
- The middle 30-60% of the script is the "value bank" — concrete tips, frames, demos.
- Save 1-3 seconds at the end for the CTA. Don't over-explain.

# 4. CTA FRAMING

CTAs must be VALUE-FIRST and SPECIFIC:
  - "Save this so you don't forget" beats "follow for more"
  - "Comment your Y for a custom Z" beats "tell us what you think"
  - When unsure, default to "Save" + niche-relevant micro-action.
Leave `cta` EMPTY when the genre doesn't naturally support one (e.g. pure
aesthetic / artistic reference).

# 5. VISUAL VARIETY

Across scenes, vary shot scale and angle. Don't repeat the same composition twice in a row.
Pick from this palette (don't all be wide-shots):
  - wide / establishing
  - medium
  - close-up
  - extreme close-up / detail
  - POV / first-person
  - over-the-shoulder
  - top-down / flat-lay
  - tracking / dolly

# 6. SOUND DESIGN

Use the `audio_mood` field to tag what kind of music or SFX accompanies each scene
(e.g. `lofi_chill`, `cinematic_tension`, `upbeat_pop`, `comedic_sting`, `silent`).
Voiceover (`voiceover`) is OPTIONAL — leave it empty for scenes that should be visual-only.
When the genre is text-on-screen (educational lists, swipe carousels), voiceover is usually empty.

# 7. ORIGINALITY GUARDRAILS

- Same genre, same emotional beats, same payoff structure — but a different topic,
  different framing, or a different angle than the reference. The goal is to ride the
  same wave without surfing on someone else's board.
- DO NOT pull caption text, on-screen text, named entities, song lyrics, or specific
  product names from the reference verbatim.
- If the brand voice (`brand voice`) is provided, lean into it: word choice, sentence
  length, energy level. If not, default to confident-friendly.

# 8. OUTPUT SCHEMA (return EXACTLY this, no markdown, no commentary)

{
  "duration_sec": number,                              // total target seconds
  "hook": string,                                      // first 1-2s gripper, ≤ 70 chars
  "cta": string,                                       // closing CTA, ≤ 60 chars (or empty)
  "music": {"mood": string, "bpm_range": [number, number]},
  "outro_template_id": string | null,                  // null unless the reference has a recognisable outro
  "scenes": [
    {
      "idx": number,                                   // 1-based
      "duration": number,                              // seconds
      "shot_type": string,                             // "wide" | "medium" | "close_up" | "extreme_close_up"
                                                       //  | "pov" | "over_shoulder" | "top_down" | "tracking"
      "image_prompt": string,                          // DELTA prompt for img2img — kept-from-source + only the differences
      "motion_prompt": string,                         // I2V motion (minimal for photo/carousel)
      "on_screen_text": string,                        // empty when none; ≤ 50 chars; ≤ 7 words
      "text_style": string,                            // "bold_white" | "subtle_caption" | "kinetic_typography"
                                                       //  | "handwritten" | "none" (when on_screen_text is empty)
      "transition_out": string,                        // "cut" | "fade" | "whip_pan_left" | "whip_pan_right"
                                                       //  | "slide" | "match_cut" | "zoom_in"
      "voiceover": string,                             // empty when scene is silent
      "audio_mood": string                             // tag matching music_tracks.mood
    }
  ]
}

# 9. HARD RULES

- Sum of `scene.duration` ≈ `duration_sec` within ±0.5 seconds.
- `image_prompt` is a DELTA prompt for img2img — see § 0. It is NOT a standalone
  scene description. The downstream Flux call already has the source frame as init.
- Keep `image_prompt` short (1-3 sentences). Long prompts cause img2img drift.
- `hook` is a SCRIPT line (what the viewer hears or reads), not a description of what
  happens. e.g. "Stop scrolling — this is what 90% of people get wrong about X"
  NOT "we open with a fast cut and the host looking surprised".
- Output strictly valid JSON. No trailing commas. No comments. No backticks.
"""


REPURPOSE_SYSTEM_PROMPT = """You are a short-form video editor working in REPURPOSE mode.

The source video has ALREADY been cut into numbered segments at its real shot
boundaries. You are NOT writing image or motion prompts — no frames are being
synthesized. Your job is to decide, per segment, whether the real footage ships
as-is, and to write the new on-screen text that rebrands it.

## 0. WHAT YOU CONTROL
- `action` per segment: keep | replace | drop
- `on_screen_text` + `text_style` per segment (this is the main rebranding lever)
- `hook`, `cta`, `duration_sec`, `music`, and the caption/hashtag guidance

## 1. ACTION RULES
- Default is `keep`. The whole point of this mode is shipping the real footage.
- `replace` ONLY when the segment would be unusable as-is: a competitor logo,
  watermark, or readable brand name fills the frame; or the segment shows a
  price/claim that is false for us. When you mark `replace`, you MUST supply
  `replace_reason` and a `replace_prompt` (a normal image-generation prompt
  describing a substitute shot that matches the segment's framing and energy).
- `drop` ONLY for dead air — a black frame, an accidental cut, a trailing
  end-card. Dropping shortens the output, so use it sparingly.

## 2. DURATIONS ARE FIXED
Each segment's duration is given to you. Echo it EXACTLY in `duration`. Do not
round, do not invent, do not "improve the pacing" by changing numbers — the
video is already cut. Changing a duration desyncs every text overlay.

## 3. ON-SCREEN TEXT
- This is where our brand voice lives. Write text that reads as OURS, not a
  transcription of the source's captions.
- One short line per segment. It must be readable in the segment's duration:
  roughly 3 words per second of segment, max ~8 words.
- Not every segment needs text. Leave `on_screen_text` empty for segments that
  carry themselves. Wall-to-wall text reads as spam.
- `text_style` ∈ {bold_center, subtitle_bottom, title_top, kinetic_typography}.
  Use `kinetic_typography` for the hook segment at most; it is expensive to read.

## 4. AUDIO
The source audio is KEPT by default — trending audio is a large part of why the
source performed, and stripping it costs reach. So:
- Set `voiceover_enabled` to false and leave every `voiceover` empty UNLESS the
  brief explicitly asks for narration.
- Set `source_audio_mode` to "keep". Use "duck" only when you also wrote
  voiceover lines; "drop" only when the brief demands our own audio.

## 5. HOOK AND CTA
`hook` is the line that goes on the FIRST segment. `cta` goes on the last one.
Both are script lines the viewer reads — not descriptions of what happens.

## 6. OUTPUT SCHEMA (strict JSON, no prose, no fences)
{
  "duration_sec": <sum of kept segment durations>,
  "hook": "<first-segment line>",
  "cta": "<last-segment line>",
  "voiceover_enabled": false,
  "source_audio_mode": "keep",
  "music": {"mood": "<mood>", "bpm_range": "<lo-hi>"},
  "outro_template_id": null,
  "scenes": [
    {
      "idx": 1,
      "segment_idx": 1,
      "action": "keep",
      "replace_reason": null,
      "replace_prompt": null,
      "duration": 2.4,
      "shot_type": "<what the real footage shows>",
      "on_screen_text": "<our line, or empty>",
      "text_style": "bold_center",
      "transition_out": "cut",
      "voiceover": "",
      "audio_mood": "<mood>"
    }
  ]
}

## 7. HARD RULES
- One scene per segment. Every segment index in the SEGMENT MAP must appear
  exactly once, including ones you mark `drop`.
- `idx` and `segment_idx` are the same number for every scene.
- `duration` must equal the segment's stated duration.
- `transition_out` is "cut" unless the source itself dissolves there.
- Output strictly valid JSON. No trailing commas. No comments. No backticks.
"""


# Instagram's media_type → human-readable kind. The analyzer uses this to
# decide whether to produce a still / slideshow / reel scenario.
_MEDIA_TYPE_LABELS = {
    1: "photo",
    2: "video",
    8: "carousel",
}


def _source_kind_label(media_type, product_type) -> str:
    """Map IG (media_type, product_type) into the analyzer's `source kind` token.

    `product_type='clips'` or `'reels'` overrides media_type=2 → "reel"
    because reel pacing is distinct from generic video posts.
    """
    if product_type in ("clips", "reels"):
        return "reel"
    if isinstance(media_type, int):
        return _MEDIA_TYPE_LABELS.get(media_type, "video")
    return "video"


def _engagement_signal(meta: dict) -> Optional[str]:
    """Cheap quality estimate the LLM can lean on when shaping pacing.

    `score` is our 0-100 composite; we bucket it into low/mid/high so the
    analyzer can prioritise "what worked here" without us shipping raw
    numbers it might misinterpret. Falls back to like_count / play_count
    when no score is available.
    """
    score = meta.get("score")
    if score is not None:
        try:
            v = float(score)
        except (TypeError, ValueError):
            v = 0.0
        if v >= 60:
            return "high — this reference performed well; lean into what made it work"
        if v >= 30:
            return "mid — solid but not viral; keep the format, sharpen the hook"
        return "low — the reference under-performed; preserve only the genre, tighten the rest"
    plays = meta.get("play_count") or meta.get("view_count") or 0
    likes = meta.get("like_count") or 0
    if plays >= 100_000 or likes >= 5_000:
        return "high engagement — preserve the core structure"
    if plays >= 10_000 or likes >= 500:
        return "mid engagement — usable template, sharpen the hook"
    return None


def build_user_prompt(reference: ContentReference, brand_style_suffix: Optional[str] = None) -> str:
    """Assemble the analyzer's user message from reference fields.

    Provides the LLM with:
      1. SOURCE KIND (drives scenario shape — see system prompt § 1)
      2. The original caption / transcript (so it understands subject + voice)
      3. ENGAGEMENT signal (bucketed) so it knows how closely to preserve format
      4. Hashtag set (signals niche + audience tone)
      5. Optional brand_style_suffix (project brand_kit voice notes)
      6. A final source-aware directive that mirrors system-prompt rules
    """
    meta = reference.metadata_json or {}
    source_kind = _source_kind_label(meta.get("media_type"), meta.get("product_type"))
    parts = [
        "# REFERENCE BRIEF",
        f"- source: {reference.source_provider}",
        # Surface the kind FIRST and in human-readable form so the
        # analyzer keys its scenario shape off it (single-scene photo
        # vs slideshow carousel vs reel) per the system prompt rules.
        f"- source kind: {source_kind}",
    ]
    if reference.source_url:
        parts.append(f"- url: {reference.source_url}")
    username = meta.get("username")
    if username:
        parts.append(f"- author: @{username}")
    if reference.caption:
        parts.append("\n## ORIGINAL CAPTION\n" + reference.caption.strip())
    if reference.transcript:
        parts.append("\n## TRANSCRIPT (do NOT quote verbatim)\n" + reference.transcript.strip())

    # Numeric signals — keep them compact, the bucketed signal below is
    # what the LLM should weight most.
    numeric_bits = []
    for key in ("duration_sec", "play_count", "view_count", "like_count", "comment_count"):
        value = meta.get(key)
        if value is not None:
            numeric_bits.append(f"{key}={value}")
    if numeric_bits:
        parts.append("\n## METRICS\n- " + " · ".join(numeric_bits))

    signal = _engagement_signal(meta)
    if signal:
        parts.append(f"- performance: {signal}")

    if reference.hashtags:
        # Hashtags expose niche, audience tone, and tag conventions.
        parts.append(
            "\n## HASHTAGS (niche signal — do NOT copy verbatim)\n"
            + " ".join(f"#{h.lstrip('#')}" for h in reference.hashtags)
        )

    if brand_style_suffix:
        parts.append(
            "\n## BRAND VOICE (lean into this — word choice, energy, sentence length)\n"
            + brand_style_suffix.strip()
        )
    # Final instruction adapts to the source kind so the analyzer
    # doesn't default to "9:16 reel" for every reference.
    if source_kind == "photo":
        parts.append(
            "\nProduce a scenario JSON for a SINGLE static frame (one scene, minimal motion) "
            "in the same genre / mood as the reference photo. Originality is mandatory — do "
            "NOT recreate specific elements verbatim."
        )
    elif source_kind == "carousel":
        parts.append(
            "\nProduce a scenario JSON for a MULTI-IMAGE SLIDESHOW (one scene per slide, "
            "minimal motion per scene) in the same genre / mood as the reference carousel."
        )
    else:
        parts.append(
            "\nProduce a scenario JSON for a 9:16 short-form video in the SAME genre/mood. "
            "Originality is mandatory — do NOT recreate specific frames or quotes from the reference."
        )
    return "\n".join(parts)


def build_repurpose_user_prompt(
    reference: ContentReference,
    segments: list,
    brand_style_suffix: Optional[str] = None,
) -> str:
    """User message for repurpose mode.

    Reuses the reference brief from `build_user_prompt` (caption,
    metrics, engagement signal, hashtags, brand voice) and replaces the
    trailing source-kind directive with the SEGMENT MAP — the numbered
    cut list the model must answer one-for-one.

    The vision images attached alongside are index-aligned with the map:
    image N is segment N's representative frame.
    """
    base = build_user_prompt(reference, brand_style_suffix=brand_style_suffix)
    # Drop the recreate-mode closing directive (always the last block).
    head = base.rsplit("\nProduce a scenario JSON", 1)[0]

    lines = [
        head,
        "\n# SEGMENT MAP (image N attached below = segment N)",
    ]
    for seg in segments:
        lines.append(
            f"SEGMENT {seg.idx}  {seg.start_sec:.2f}–{seg.end_sec:.2f}s  "
            f"({seg.duration_sec:.2f}s)"
        )
    total = sum(s.duration_sec for s in segments)
    lines.append(
        f"\nTotal source runtime: {total:.2f}s across {len(segments)} segments."
    )
    lines.append(
        "\nProduce the repurpose scenario JSON. One scene per segment above, in "
        "order, with `duration` echoed exactly. Default every action to `keep`; "
        "mark `replace` only for the specific reasons in § 1. Keep the source "
        "audio — write on-screen text, not narration."
    )
    return "\n".join(lines)


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


_REPURPOSE_ACTIONS = ("keep", "replace", "drop")


def _validate_repurpose(payload: dict, expected_indices: Optional[set] = None) -> dict:
    """Repurpose scenes describe edit decisions, not synthesis prompts."""
    scenes = payload["scenes"]
    seen: set[int] = set()
    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise ValueError(f"scenes[{i}] is not an object")
        for field in ("idx", "segment_idx", "action", "duration"):
            if field not in scene:
                raise ValueError(f"scenes[{i}] missing required field: {field}")
        action = scene.get("action")
        if action not in _REPURPOSE_ACTIONS:
            raise ValueError(
                f"scenes[{i}].action must be one of {_REPURPOSE_ACTIONS}, got {action!r}"
            )
        if action == "replace" and not (scene.get("replace_prompt") or "").strip():
            raise ValueError(f"scenes[{i}].action='replace' requires replace_prompt")
        try:
            seen.add(int(scene["segment_idx"]))
        except (TypeError, ValueError):
            raise ValueError(f"scenes[{i}].segment_idx is not an integer") from None

    if expected_indices is not None and seen != expected_indices:
        missing = sorted(expected_indices - seen)
        extra = sorted(seen - expected_indices)
        raise ValueError(
            "scenes must cover every planned segment exactly once "
            f"(missing={missing}, unexpected={extra})"
        )
    return payload


def validate_scenario(
    payload: dict,
    *,
    mode: str = "recreate",
    expected_segment_indices: Optional[set] = None,
) -> dict:
    """Lightweight shape validation. Returns the (possibly normalized) payload.

    Strict pydantic validation lives on the Scenario edit endpoint; here we
    just guarantee the analyzer didn't return garbage. `mode` picks the
    per-scene field set: recreate/brand_build scenes carry synthesis
    prompts, repurpose scenes carry edit decisions.
    """
    if not isinstance(payload, dict):
        raise ValueError("scenario JSON is not an object")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("scenario.scenes must be a non-empty array")

    if mode == "repurpose":
        return _validate_repurpose(payload, expected_segment_indices)

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
    production_mode: str = "recreate",
    segments: Optional[list] = None,
) -> tuple[dict, LLMResponse]:
    """Run the analyzer end-to-end. Returns (scenario_json, llm_response).

    Caller is responsible for writing the `generation_calls` row and updating
    the scenario row — keeps this function easy to unit test with fakes.

    In `repurpose` mode the caller must pass the planned `segments`; the
    prompt and the validation both key off that cut list.
    """
    is_repurpose = production_mode == "repurpose" and segments
    if is_repurpose:
        user_prompt = build_repurpose_user_prompt(
            reference, segments, brand_style_suffix=brand_style_suffix
        )
        system_prompt = REPURPOSE_SYSTEM_PROMPT
        expected = {s.idx for s in segments}
    else:
        user_prompt = build_user_prompt(reference, brand_style_suffix=brand_style_suffix)
        system_prompt = SYSTEM_PROMPT
        expected = None

    response = await provider.complete(
        prompt=user_prompt,
        route=route,
        vision_inputs=vision_inputs,
        system=system_prompt,
    )
    try:
        scenario = parse_scenario_json(response.text)
        scenario = validate_scenario(
            scenario,
            mode="repurpose" if is_repurpose else production_mode,
            expected_segment_indices=expected,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("analyzer_parse_failed", error=str(exc), preview=response.text[:300])
        raise
    return scenario, response
