"""Platform / aspect presets — single source of truth for compose specs.

Lives in code (not DB) because these specs change rarely and pinning them
in version control prevents an accidental admin edit from breaking final
renders. CP-M3 uses ASPECT_DIMENSIONS only; CP-M5 will read the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SafeZones:
    """Pixels reserved on the platform's UI overlay (don't put text here)."""

    top: int = 0
    bottom: int = 0
    left: int = 0
    right: int = 0


@dataclass(frozen=True)
class VariantPreset:
    """Platform-spec output settings for a render variant."""

    aspect: str  # canonical aspect ratio (matches `aspect_ratio` columns)
    width: int
    height: int
    fps: int = 30
    max_duration: int = 90
    audio_lufs: int = -14
    container: str = "mp4"
    safe_zones: SafeZones = field(default_factory=SafeZones)


PRESETS: dict[str, VariantPreset] = {
    "ig_reels": VariantPreset(aspect="9:16", width=1080, height=1920, max_duration=90),
    "tiktok": VariantPreset(
        aspect="9:16", width=1080, height=1920, max_duration=600, safe_zones=SafeZones(top=130, bottom=280)
    ),
    "ig_story": VariantPreset(
        aspect="9:16", width=1080, height=1920, max_duration=60, safe_zones=SafeZones(top=250, bottom=250)
    ),
    "ig_feed_45": VariantPreset(aspect="4:5", width=1080, height=1350, max_duration=60),
    "ig_feed_11": VariantPreset(aspect="1:1", width=1080, height=1080),
    "yt_shorts": VariantPreset(aspect="9:16", width=1080, height=1920, max_duration=60),
}


# Aspect group → master dimensions used by image_gen. Variants in the same
# aspect group share these dimensions, then compose crops/letterboxes to the
# variant-specific safe-zone layout.
ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

# Reverse map: which aspect group does each variant fall into.
VARIANT_ASPECT_GROUP: dict[str, str] = {key: preset.aspect for key, preset in PRESETS.items()}


def aspect_dimensions(aspect: str) -> tuple[int, int]:
    """Return (width, height) for an aspect ratio. Raises KeyError on unknown."""
    return ASPECT_DIMENSIONS[aspect]


def variant_aspect(variant_key: str) -> Optional[str]:
    return VARIANT_ASPECT_GROUP.get(variant_key)


def recommend_preset_for_reference(reference) -> str:
    """Pick a sensible default output preset from a reference's source kind.

    A remake targets ONE preset. Reels/clips → `ig_reels` (9:16); feed
    video / carousel / photo → `ig_feed_45` (4:5); anything unknown →
    `ig_reels`, the most ubiquitous short-form slot.
    """
    meta = (getattr(reference, "metadata_json", None) or {})
    media_type = meta.get("media_type")
    product_type = (meta.get("product_type") or "").lower()

    if product_type in ("clips", "reels"):
        return "ig_reels"
    if media_type in (1, 2, 8):  # photo / video / carousel feed post
        return "ig_feed_45"
    return "ig_reels"
