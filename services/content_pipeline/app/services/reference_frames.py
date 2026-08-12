"""Map scenario scenes to reference init frames for img2img.

Phase 4 — the default `recreate` mode now seeds each scene_render with
a frame from the SOURCE reference. The text prompt becomes a "delta"
on that frame rather than a from-scratch description; output quality
jumps because Flux has a real composition to anchor on instead of
inventing one from words.

Source kind → frame source:
  - **photo**         → the single mirrored image (same key for every scene)
  - **carousel (N)**  → `metadata.slide_s3_keys[i]` per scene (wrap mod N)
  - **reel / video**  → `metadata.frame_s3_keys[i]` (populated async by
                        `reference_frame_extract`); falls back to the
                        poster while extraction is still pending
  - **manual_upload** → reuses photo / reel handling based on mime

Callers (today only `scene_renders.materialize_for_scenario`) get a
plain `list[Optional[str]]` indexed by scene_idx — None means "no init
frame available, fall back to pure t2i".
"""

from __future__ import annotations

from typing import List, Optional

from app.models.content_references import ContentReference


def _carousel_keys(meta: dict) -> List[Optional[str]]:
    """Pull mirrored carousel slide keys. None entries are kept so the
    caller can wrap around the populated ones with a modulo index."""
    raw = meta.get("slide_s3_keys")
    if isinstance(raw, list):
        return [
            k if isinstance(k, str) and k else None for k in raw
        ]
    return []


def _reel_frame_keys(meta: dict) -> List[Optional[str]]:
    raw = meta.get("frame_s3_keys")
    if isinstance(raw, list):
        return [
            k if isinstance(k, str) and k else None for k in raw
        ]
    return []


def compute_init_keys(reference: ContentReference, scene_count: int) -> List[Optional[str]]:
    """Return `scene_count` init image S3 keys, one per scene_idx.

    Always returns a list of length `scene_count`. Slots map 1:1 with
    `scenarios.scenario_json.scenes[i].idx` (i = 0..scene_count-1).
    A None slot means image_gen should fall through to pure t2i for
    that cell — never raise just because a single frame is missing.
    """
    if scene_count <= 0:
        return []

    meta = reference.metadata_json or {}
    media_type = meta.get("media_type")
    product_type = (meta.get("product_type") or "").lower()
    is_reel = media_type == 2 or product_type in ("clips", "reels")
    is_carousel = media_type == 8

    out: List[Optional[str]] = []

    if is_carousel:
        slides = _carousel_keys(meta)
        # Fallback to media_s3_key (slide 0) when slide_s3_keys is
        # absent or empty.
        if not any(slides):
            slides = [reference.media_s3_key] if reference.media_s3_key else []
        if not slides:
            return [None] * scene_count
        for i in range(scene_count):
            # Wrap around when scenes > slides: lets a 4-slide carousel
            # feed a 6-scene scenario without making slides 5-6 blank.
            slot = slides[i % len(slides)]
            out.append(slot)
        return out

    if is_reel:
        frames = _reel_frame_keys(meta)
        # Extraction may still be in flight; degrade gracefully to the
        # poster so SOMETHING anchors the prompt. Better a slightly off
        # frame than no init at all.
        if not any(frames):
            fallback = reference.poster_s3_key or reference.media_s3_key
            return [fallback] * scene_count if fallback else [None] * scene_count
        for i in range(scene_count):
            slot = frames[i % len(frames)]
            if slot is None:
                slot = reference.poster_s3_key
            out.append(slot)
        return out

    # Photo / manual image fallback — single image, repeat for every scene.
    single = reference.media_s3_key or reference.poster_s3_key
    return [single] * scene_count


# Sensible default img2img strength when nothing else is set. Tuned
# empirically: 0.55-0.65 keeps the reference's subject + composition
# recognisable while letting the prompt impose camera / lighting /
# styling changes. Admin can override per-scenario via
# `model_routes.params.image_strength`.
DEFAULT_REFERENCE_STRENGTH = 0.55
