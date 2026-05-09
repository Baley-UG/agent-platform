"""Caption / hashtag resolution.

Order of precedence:
1. plan_slot.caption_override / hashtags_override
2. scenario.default_caption / default_hashtags
3. derive from scenario_json.cta + scenario_json.hashtags (CP-M8)
4. empty string + empty list

Hashtags are joined to the caption with a leading newline. Returns the
single text the publisher hands to IG/TT.
"""

from __future__ import annotations

from typing import List, Optional

from app.models.plan_slots import PlanSlot
from app.models.scenarios import Scenario


def _coerce_hashtags(items: Optional[List[str]]) -> List[str]:
    if not items:
        return []
    out: List[str] = []
    seen: set = set()
    for raw in items:
        token = (raw or "").strip().lstrip("#")
        if not token:
            continue
        token_lower = token.lower()
        if token_lower in seen:
            continue
        seen.add(token_lower)
        out.append(f"#{token}")
    return out


def resolve(slot: Optional[PlanSlot], scenario: Optional[Scenario]) -> str:
    """Build the publish-ready caption string from slot + scenario fields."""
    caption: Optional[str] = None
    hashtags: List[str] = []

    if slot is not None:
        caption = caption or (slot.caption_override or None)
        if slot.hashtags_override:
            hashtags = _coerce_hashtags(slot.hashtags_override)

    if scenario is not None:
        caption = caption or (scenario.default_caption or None)
        if not hashtags and scenario.default_hashtags:
            hashtags = _coerce_hashtags(scenario.default_hashtags)

    body = (caption or "").strip()
    tail = " ".join(hashtags)
    if body and tail:
        return f"{body}\n\n{tail}"
    return body or tail
