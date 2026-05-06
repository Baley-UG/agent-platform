"""Pre-fetch filter — decides whether a candidate media is worth deep
scraping (comments, etc).

Rule (matches plan § 7):
    A post passes iff
      (min_likes is None        or like_count   >= min_likes) AND
      (min_impressions is None  or impressions  >= min_impressions) AND
      (since is None            or taken_at     >= since)

`impressions` is `play_count` if available, else `view_count`, else 0.
For photo-only posts neither is exposed, so a `min_impressions` set
against a photo target will skip everything — documented as a "best-
effort" caveat in the plan.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class FilterResult:
    """Why a post passed or was skipped — useful for stats accumulation."""

    passed: bool
    reason: Optional[str] = None  # "below_min_likes" | "below_min_impressions" | "before_since"


def passes_filter(
    *,
    like_count: int,
    play_count: Optional[int],
    view_count: Optional[int],
    taken_at: datetime,
    min_likes: Optional[int],
    min_impressions: Optional[int],
    since: Optional[datetime],
) -> FilterResult:
    """Apply the three-clause filter. All thresholds are optional."""
    if min_likes is not None and like_count < min_likes:
        return FilterResult(False, "below_min_likes")
    if min_impressions is not None:
        impressions = play_count if play_count is not None else (view_count or 0)
        if impressions < min_impressions:
            return FilterResult(False, "below_min_impressions")
    if since is not None and taken_at < since:
        return FilterResult(False, "before_since")
    return FilterResult(True)
