"""Vision auto-tagger for brand assets.

When a brand asset lands (admin upload, or admin re-tag request), the
worker runs a small vision-LLM call against the asset thumbnail and
writes structured tags onto the `media_assets` row.

Goals:
- Zero-effort onboarding: admin uploads 50 assets, system tags them
  while they're getting coffee. Manual tagging doesn't scale.
- Stable JSON output: we constrain the LLM with a strict prompt + JSON
  parse fallbacks. Tagger output is always shape-validated before write.
- Fail-open: a tag failure NEVER blocks an upload. The asset stays in
  the library un-tagged; admin can edit manually or trigger a retag.

The vision-LLM call goes through the same `scenario_analysis` route as
the analyzer — admins don't have to add a separate model_routes entry.
We don't ship a dedicated "tagger" route key today; if admins want to
swap models, set the project's scenario_analysis route.
"""

from __future__ import annotations

import base64
import io as _io
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logging import logger
from app.core import s3 as s3lib
from app.models.media_assets import MediaAsset
from app.models.projects import Project
from app.services import model_router
from app.services.providers.llm.base import LLMProvider, VisionInput
from app.services.providers.llm.openrouter import OpenRouterProvider


SYSTEM_PROMPT = """You are a visual-asset tagger for a brand content library.
Look at the attached image and return STRICT JSON describing what you see.

Pick the brand_asset_type from this exact list:
  - hero_product: product as the clear hero of the frame
  - lifestyle:    product or brand in real-life use, ambient
  - face_talking: a person clearly facing camera, likely speaking
  - face_action:  a person doing something (using product, walking, etc.)
  - logo_card:    logo dominates the frame (brand mark + maybe wordmark)
  - text_card:    typography-dominant frame, minimal imagery
  - b_roll:       supporting / atmospheric shot, no clear product hero
  - product_detail: extreme close-up showing a product detail
  - misc:         doesn't fit any of the above

Other fields:
- mood: ONE word — luxe | energetic | calm | playful | minimal | warm | cold | bold | nostalgic
- dominant_colors: 3-5 hex strings (#rrggbb), sampled from the frame, ordered by area
- subjects: short noun list of what is visible (max 6 entries, lowercase)
- has_face: true ONLY if a clear, identifiable human face is visible
- motion_intensity (videos only; omit for images): still | slow | energetic

Return ONLY the JSON object. No prose, no markdown, no commentary."""


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _strip_fences(text: str) -> str:
    """Tolerate ```json … ``` fences that some LLMs add despite instructions."""
    return _JSON_FENCE_RE.sub("", text).strip()


def _parse_tagger_response(text: str) -> dict:
    """Parse LLM output into a dict. Raises ValueError on unrecoverable shape.

    Defensive against:
      - leading/trailing prose ("Here is the JSON:")
      - markdown fences
      - trailing commas (regex-strip)
    """
    cleaned = _strip_fences(text).strip()
    # Drop everything before the first "{" — some models prepend chatter.
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]
    # Drop everything after the LAST "}".
    last = cleaned.rfind("}")
    if last >= 0:
        cleaned = cleaned[: last + 1]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tagger returned non-JSON: {cleaned[:200]}") from exc


_VALID_TYPES = {
    "hero_product",
    "lifestyle",
    "face_talking",
    "face_action",
    "logo_card",
    "text_card",
    "b_roll",
    "product_detail",
    "misc",
}
_VALID_MOOD_INTENSITY = {"still", "slow", "energetic"}


def _coerce_tags(raw: dict) -> tuple[Optional[str], dict]:
    """Pull the type out, normalize the rest into a tags dict.

    Returns `(brand_asset_type, tags_dict)`. Missing/invalid fields drop
    silently — strict mode would reject perfectly usable taggings just
    because the LLM emitted an extra key.
    """
    asset_type: Optional[str] = None
    raw_type = raw.get("brand_asset_type")
    if isinstance(raw_type, str) and raw_type.strip() in _VALID_TYPES:
        asset_type = raw_type.strip()

    tags: dict = {}
    if isinstance(raw.get("mood"), str):
        tags["mood"] = raw["mood"].strip().lower()
    colors = raw.get("dominant_colors")
    if isinstance(colors, list):
        tags["dominant_colors"] = [
            c.strip()
            for c in colors
            if isinstance(c, str) and c.strip().startswith("#")
        ][:5]
    subjects = raw.get("subjects")
    if isinstance(subjects, list):
        tags["subjects"] = [
            s.strip().lower() for s in subjects if isinstance(s, str) and s.strip()
        ][:6]
    if isinstance(raw.get("has_face"), bool):
        tags["has_face"] = raw["has_face"]
    motion = raw.get("motion_intensity")
    if isinstance(motion, str) and motion.strip().lower() in _VALID_MOOD_INTENSITY:
        tags["motion_intensity"] = motion.strip().lower()
    extra_tags = raw.get("tags")
    if isinstance(extra_tags, list):
        tags["tags"] = [
            t.strip() for t in extra_tags if isinstance(t, str) and t.strip()
        ][:10]

    return asset_type, tags


def _vision_input_for_asset(asset: MediaAsset) -> Optional[VisionInput]:
    """Download the asset bytes and base64-inline for the LLM.

    Same trick as the analyzer: OpenRouter cannot fetch our private
    MinIO host, so we pay the upload cost once per tag.

    For video assets we'd want to extract a frame first — Phase 1 only
    auto-tags image assets. Video tagging falls through to "tag with
    s3_key as best-guess type"; the admin can edit manually or wait for
    Phase 3 (frame extraction).
    """
    mime = (asset.mime_type or "").lower()
    if not mime.startswith("image/"):
        logger.info(
            "brand_asset_tagger_skipping_non_image",
            asset_id=str(asset.id),
            mime=mime,
        )
        return None
    if not s3lib.is_configured():
        return None
    try:
        buf = _io.BytesIO()
        s3lib.client().download_fileobj(settings.S3_BUCKET, asset.s3_key, buf)
        data = buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brand_asset_tagger_download_failed",
            asset_id=str(asset.id),
            error=str(exc),
        )
        return None
    return VisionInput(
        base64=base64.b64encode(data).decode("ascii"),
        mime_type=mime,
    )


def _build_provider(provider_name: str) -> LLMProvider:
    """Map provider name → concrete impl. Mirrors the analyzer worker."""
    if provider_name == "openrouter":
        return OpenRouterProvider()
    raise NotImplementedError(f"LLM provider not implemented: {provider_name}")


async def tag_asset(
    *,
    asset: MediaAsset,
    project: Project,
    session,  # sqlmodel Session; typed loosely to avoid import cycle here
) -> tuple[Optional[str], Optional[dict], Optional[float], Optional[int]]:
    """Run the vision tagger against `asset`. Returns
    `(brand_asset_type, brand_asset_tags, cost_usd, latency_ms)`.

    Side effects: NONE. The caller commits to DB; this function is pure
    LLM + parsing so it stays unit-testable.
    """
    vision = _vision_input_for_asset(asset)
    if vision is None:
        return None, None, None, None

    route = model_router.resolve(session, "scenario_analysis", project.id)
    if route is None:
        logger.warning(
            "brand_asset_tagger_no_route", project_id=str(project.id)
        )
        return None, None, None, None

    provider = _build_provider(route.provider)
    started = time.monotonic()
    response = await provider.complete(
        prompt="Tag this asset. Return JSON only.",
        route=route,
        vision_inputs=[vision],
        system=SYSTEM_PROMPT,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        raw = _parse_tagger_response(response.text)
    except ValueError as exc:
        logger.warning(
            "brand_asset_tagger_parse_failed",
            asset_id=str(asset.id),
            error=str(exc),
        )
        return None, None, response.cost_usd, latency_ms

    asset_type, tags = _coerce_tags(raw)
    logger.info(
        "brand_asset_tagged",
        asset_id=str(asset.id),
        brand_asset_type=asset_type,
        tags=tags,
        cost_usd=response.cost_usd,
    )
    return asset_type, tags or None, response.cost_usd, latency_ms


def apply_tagger_result(
    *,
    asset: MediaAsset,
    brand_asset_type: Optional[str],
    brand_asset_tags: Optional[dict],
) -> MediaAsset:
    """Persist tagger output to the asset row (caller commits)."""
    # NEVER overwrite an admin-pinned type. If the row already has a
    # type set BEFORE the tag job fired, leave it.
    if asset.brand_asset_type is None and brand_asset_type:
        asset.brand_asset_type = brand_asset_type

    # Merge tags rather than replace — admin may have hand-edited
    # `tags` (custom labels) while waiting for the tagger.
    merged: dict = dict(asset.brand_asset_tags or {})
    if brand_asset_tags:
        for key, value in brand_asset_tags.items():
            # Don't trample admin-edited custom tags list.
            if key == "tags" and merged.get("tags"):
                # union, preserving admin order
                seen = set(merged["tags"])
                merged["tags"] = list(merged["tags"]) + [
                    t for t in value if t not in seen
                ]
            else:
                merged.setdefault(key, value)
                # Only fill missing keys; admin overrides win.
                if key not in (asset.brand_asset_tags or {}):
                    merged[key] = value
    if merged:
        asset.brand_asset_tags = merged

    asset.auto_tagged_at = datetime.now(timezone.utc)
    return asset
