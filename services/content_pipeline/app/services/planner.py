"""Weekly plan skeleton generator + auto-fill modes + stock view.

Skeleton math is pure — `expand_preferred_slots(strategy, week_start)`
takes the JSONB rules and returns timezone-aware datetime objects for
the requested week. Tests pin this function so timezone handling and
"daily HH:MM" / "Mon HH:MM" parsing don't drift.

Fill strategies (`posting_strategy.fill_strategy`):
- `manual`     — skeleton only, admin populates by hand.
- `auto_suggest` (default) — for each empty slot, write 2-3 stock
  candidates into `plan_slots.suggested_variant_ids`. Admin clicks one
  to assign.
- `auto_fill`  — pop the best stock variant and assign it. FIFO over
  approved variants of the matching preset.

Stock view: a virtual queryset over `remakes` where status='done' and
the remake isn't already pinned to a non-failed/non-skipped slot.
`plan_slots.variant_id` now points at a remake id.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models.plan_slots import CONTENT_TYPES, PlanSlot
from app.models.posting_strategy import PostingStrategy
from app.models.remakes import Remake
from app.models.weekly_plans import WeeklyPlan


# ----- Slot expression parsing -----

_DAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(token: str) -> Optional[time]:
    m = _TIME_RE.match(token.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return time(hh, mm)


def parse_slot_expression(expr: str) -> List[Tuple[Optional[int], time]]:
    """Parse a single preferred-slots expression.

    Forms (case-insensitive):
      "daily 19:00"         → [(None, 19:00)] meaning every weekday
      "Mon 12:00"           → [(0, 12:00)]
      "Mon,Wed 19:00"       → [(0, 19:00), (2, 19:00)]
      "weekdays 09:00"      → Mon-Fri
      "weekends 11:00"      → Sat-Sun

    Unknown forms return []; the planner ignores them rather than failing
    a whole week's generation over a typo.
    """
    parts = expr.strip().rsplit(" ", 1)
    if len(parts) != 2:
        return []
    days_part, time_part = parts
    t = _parse_hhmm(time_part)
    if t is None:
        return []

    days_part_lc = days_part.strip().lower()
    if days_part_lc == "daily":
        return [(d, t) for d in range(7)]
    if days_part_lc == "weekdays":
        return [(d, t) for d in range(0, 5)]
    if days_part_lc == "weekends":
        return [(d, t) for d in (5, 6)]

    out: List[Tuple[Optional[int], time]] = []
    for token in days_part_lc.split(","):
        d = _DAY_NAMES.get(token.strip())
        if d is None:
            continue
        out.append((d, t))
    return out


# ----- Skeleton generation -----

# Variant preset → content_type. Keep this aligned with PRESETS in services/presets.py.
VARIANT_TO_CONTENT_TYPE = {
    "ig_reels": "reel",
    "ig_story": "story",
    "tiktok": "tiktok_video",
    "yt_shorts": "reel",
    "ig_feed_45": "post",
    "ig_feed_11": "post",
}


def expand_preferred_slots(
    strategy: PostingStrategy,
    week_start_date: date,
) -> List[Tuple[datetime, str, str]]:
    """Expand `preferred_slots × weekly_quota` into a list of slot tuples.

    Returns [(scheduled_at, variant_preset, content_type), ...] sorted by
    scheduled_at. Each variant preset's tuples are capped at
    `weekly_quota[preset]` — extra preferred_slots beyond the quota are
    dropped (FIFO of preferred_slots' declared order).

    Times are interpreted in the project's `posting_strategy.timezone`,
    then converted to UTC for storage.
    """
    try:
        tzinfo = ZoneInfo(strategy.timezone)
    except Exception:  # noqa: BLE001
        tzinfo = ZoneInfo("UTC")

    out: List[Tuple[datetime, str, str]] = []
    quota_map = strategy.weekly_quota or {}
    preferred_map = strategy.preferred_slots or {}

    for preset, expressions in preferred_map.items():
        if not isinstance(expressions, list):
            continue
        quota = int(quota_map.get(preset, 0))
        if quota <= 0:
            continue
        content_type = VARIANT_TO_CONTENT_TYPE.get(preset, "post")
        emitted = 0
        for expr in expressions:
            if emitted >= quota:
                break
            for (weekday, hhmm) in parse_slot_expression(str(expr)):
                if emitted >= quota:
                    break
                if weekday is None:
                    continue
                slot_date = week_start_date + timedelta(days=weekday)
                local_dt = datetime.combine(slot_date, hhmm, tzinfo=tzinfo)
                utc_dt = local_dt.astimezone(timezone.utc)
                out.append((utc_dt, preset, content_type))
                emitted += 1

    out.sort(key=lambda row: row[0])
    return out


def is_in_blackout(scheduled_at: datetime, blackout: dict, tzinfo: ZoneInfo) -> bool:
    """Apply blackout windows (`{ "Sat": ["00:00-08:00"], "daily": [...] }`)."""
    if not blackout:
        return False
    local = scheduled_at.astimezone(tzinfo)
    weekday_key = local.strftime("%a")  # Mon, Tue, ...
    minutes = local.hour * 60 + local.minute
    for key in (weekday_key, "daily"):
        windows = blackout.get(key)
        if not windows:
            continue
        for window in windows:
            try:
                start_s, end_s = str(window).split("-")
                sh, sm = map(int, start_s.strip().split(":"))
                eh, em = map(int, end_s.strip().split(":"))
            except (ValueError, AttributeError):
                continue
            start = sh * 60 + sm
            end = eh * 60 + em
            if start <= minutes < end:
                return True
    return False


def respect_min_gap(
    candidate: datetime, preset: str, already: Iterable[datetime], min_gap_minutes: dict
) -> bool:
    gap = int((min_gap_minutes or {}).get(preset, 0))
    if gap <= 0:
        return True
    threshold = timedelta(minutes=gap)
    return all(abs(candidate - other) >= threshold for other in already)


# ----- Stock view -----


def stock_for_preset(
    session: Session, project_id: uuid.UUID, preset_key: str, *, limit: int = 50
) -> List[Remake]:
    """Done remakes for `preset_key` not yet pinned to an active slot."""
    pinned_subq = select(PlanSlot.variant_id).where(
        PlanSlot.project_id == project_id,
        PlanSlot.variant_id.is_not(None),
        PlanSlot.status.notin_(("failed", "skipped")),
    )
    stmt = (
        select(Remake)
        .where(
            Remake.preset_key == preset_key,
            Remake.status == "done",
            Remake.final_media_asset_id.is_not(None),
            Remake.id.notin_(pinned_subq),
        )
        .order_by(Remake.final_approved_at.asc().nulls_last(), Remake.created_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def stock_for_project(session: Session, project_id: uuid.UUID) -> List[Remake]:
    """All done+unpinned remakes for a project, any preset."""
    pinned_subq = select(PlanSlot.variant_id).where(
        PlanSlot.project_id == project_id,
        PlanSlot.variant_id.is_not(None),
        PlanSlot.status.notin_(("failed", "skipped")),
    )
    stmt = (
        select(Remake)
        .where(
            Remake.status == "done",
            Remake.final_media_asset_id.is_not(None),
            Remake.id.notin_(pinned_subq),
        )
        .order_by(Remake.final_approved_at.asc().nulls_last(), Remake.created_at.asc())
    )
    return list(session.exec(stmt).all())


# ----- Fill strategies -----


def suggest_for_slot(
    session: Session, slot: PlanSlot, *, k: int = 3
) -> List[uuid.UUID]:
    """Top-k stock candidates for a slot's preset (auto_suggest mode)."""
    candidates = stock_for_preset(session, slot.project_id, slot.variant_preset, limit=k)
    return [v.id for v in candidates]


def auto_fill_slot(session: Session, slot: PlanSlot) -> Optional[Remake]:
    """Pop the best stock variant for this slot and pin it (auto_fill mode)."""
    candidates = stock_for_preset(session, slot.project_id, slot.variant_preset, limit=1)
    if not candidates:
        return None
    chosen = candidates[0]
    slot.variant_id = chosen.id
    slot.source_kind = "stock"
    slot.status = "ready"
    session.add(slot)
    session.flush()
    return chosen


# ----- Helpers for the API/scheduler -----


def monday_of(d: date) -> date:
    """ISO Monday of any date (Monday→Monday, Tuesday→same Monday, etc.)."""
    return d - timedelta(days=d.weekday())
