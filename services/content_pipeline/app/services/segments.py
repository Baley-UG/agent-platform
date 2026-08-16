"""Segment planning for `repurpose` production mode.

Repurpose skips AI synthesis entirely: the output video is built from
real segments cut out of the source reel. This module turns a
reference's detected shot boundaries into a **cut list** — one segment
per output scene, with exact start/end timestamps.

Why this matters beyond "we want the real footage": in the legacy
pipeline `scenes[].duration` is an LLM guess and Seedance returns clips
of some other length, so every downstream timing calculation
(`_scene_offsets`, drawtext `enable=between` windows, per-scene
voiceover offsets) is computed against a number that doesn't match the
video. Here `duration == end_sec - start_sec` by construction, so the
timing is exact for the first time.

Plan JSON shape (persisted on `scenarios.segment_plan`, admin-editable
via `PATCH /scenarios/{id}/segment-plan`):

    {
      "source_reference_id": "…",
      "source_media_s3_key": "projects/…/references/….mp4",
      "source_duration_sec": 14.2,
      "fit_mode": "cover",
      "source_audio_mode": "keep",
      "segments": [
        {"idx": 1, "start_sec": 0.0, "end_sec": 2.4,
         "frame_s3_key": "references/<id>/frames/00.jpg",
         "action": "keep", "replace_prompt": null, "replace_asset_id": null}
      ]
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.logging import logger

# Clamps tuned for short-form (5-60s) reels. Sub-1.2s scenes read as a
# glitch rather than a shot; >5s on a 15s reel kills the pacing that
# made the source work in the first place.
MIN_SEGMENT_SEC = 1.2
MAX_SEGMENT_SEC = 5.0
MAX_SEGMENTS = 12

# Used only when the source has no probed duration AND no boundaries —
# mirrors the fallback in `video_frames.extract_keyframes`.
_FALLBACK_BOUNDARIES = [2.5, 5.0, 7.5]
_FALLBACK_DURATION = 10.0

ACTIONS = ("keep", "replace", "drop")
FIT_MODES = ("cover", "contain")
SOURCE_AUDIO_MODES = ("keep", "duck", "drop")


@dataclass
class Segment:
    """One output scene, backed by a window of the source video."""

    idx: int  # 1-based; maps 1:1 onto scenario_json.scenes[].idx
    start_sec: float
    end_sec: float
    frame_s3_key: Optional[str] = None  # nearest extracted keyframe (vision + UI)
    action: str = "keep"  # keep | replace | drop
    replace_prompt: Optional[str] = None
    replace_asset_id: Optional[str] = None
    match_reason: Optional[str] = None

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 3)

    def to_json(self) -> Dict[str, Any]:
        return {
            "idx": self.idx,
            "start_sec": round(self.start_sec, 3),
            "end_sec": round(self.end_sec, 3),
            "duration_sec": self.duration_sec,
            "frame_s3_key": self.frame_s3_key,
            "action": self.action,
            "replace_prompt": self.replace_prompt,
            "replace_asset_id": self.replace_asset_id,
            "match_reason": self.match_reason,
        }


# ---------------------------------------------------------------------------
# boundary → interval math (pure; unit-tested without ffmpeg or a DB)
# ---------------------------------------------------------------------------


def _merge_short(bounds: List[float], min_sec: float) -> List[float]:
    """Drop boundaries that would create an interval shorter than
    `min_sec`, merging that interval into its predecessor.

    The last interval is special-cased: when it ends up too short we
    drop the boundary BEFORE it instead, so the tail merges backwards
    rather than leaving a stub.
    """
    if len(bounds) < 3:
        return bounds
    out = [bounds[0]]
    for b in bounds[1:-1]:
        if b - out[-1] >= min_sec:
            out.append(b)
    out.append(bounds[-1])
    # Tail stub: merge backwards.
    while len(out) > 2 and out[-1] - out[-2] < min_sec:
        del out[-2]
    return out


def _split_long(bounds: List[float], max_sec: float) -> List[float]:
    """Split any interval longer than `max_sec` into equal sub-intervals."""
    out = [bounds[0]]
    for prev, cur in zip(bounds, bounds[1:]):
        span = cur - prev
        if span > max_sec:
            pieces = int(span // max_sec) + 1
            step = span / pieces
            out.extend(prev + step * i for i in range(1, pieces))
        out.append(cur)
    return out


def _cap_count(bounds: List[float], max_count: int) -> List[float]:
    """Merge the shortest interval into a neighbour until the interval
    count fits `max_count`."""
    while len(bounds) - 1 > max_count:
        spans = [(bounds[i + 1] - bounds[i], i) for i in range(len(bounds) - 1)]
        _, shortest = min(spans)
        # Drop the boundary that ends the shortest interval, unless it
        # is the final one (then drop the boundary that starts it).
        drop = shortest + 1 if shortest + 1 < len(bounds) - 1 else shortest
        del bounds[drop]
    return bounds


def compute_intervals(
    boundaries: List[float],
    duration: float,
    *,
    min_sec: float = MIN_SEGMENT_SEC,
    max_sec: float = MAX_SEGMENT_SEC,
    max_count: int = MAX_SEGMENTS,
) -> List[tuple[float, float]]:
    """Turn raw shot boundaries + duration into `(start, end)` windows.

    Order matters: merge shorts first (so a burst of rapid cuts collapses
    into one usable shot), then split longs, then cap the count.
    """
    if duration <= 0:
        return []
    inner = sorted({round(b, 3) for b in boundaries if 0.0 < b < duration})
    bounds = [0.0, *inner, round(duration, 3)]
    bounds = _merge_short(bounds, min_sec)
    bounds = _split_long(bounds, max_sec)
    bounds = _cap_count(bounds, max_count)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _nearest_frame_key(frame_records: List[dict], start: float, end: float) -> Optional[str]:
    """Pick the extracted keyframe closest to the middle of the window."""
    if not frame_records:
        return None
    mid = (start + end) / 2
    best = min(
        frame_records,
        key=lambda r: abs(float(r.get("timestamp_sec") or 0.0) - mid),
    )
    return best.get("s3_key")


# ---------------------------------------------------------------------------
# reference → plan
# ---------------------------------------------------------------------------


def _reference_boundaries(meta: dict) -> tuple[List[float], float]:
    """Resolve the boundary list + duration from reference metadata.

    Fallback ladder:
      1. `scene_boundaries_sec` + `duration_sec_probed` — the real thing,
         stamped by `reference_frame_extract`.
      2. `frame_records` timestamps — a thumbnail set, not a cut list,
         but better than nothing for references extracted before
         repurpose mode existed.
      3. Fixed 2.5s intervals over an assumed 10s.
    """
    boundaries = meta.get("scene_boundaries_sec")
    duration = float(meta.get("duration_sec_probed") or 0.0)

    if isinstance(boundaries, list) and boundaries and duration > 0:
        return [float(b) for b in boundaries], duration

    records = meta.get("frame_records") or []
    if isinstance(records, list) and records:
        stamps = [float(r.get("timestamp_sec") or 0.0) for r in records]
        stamps = [s for s in stamps if s > 0.0]
        if stamps:
            if duration <= 0:
                # No probe — assume the last frame sits one typical shot
                # before the end.
                duration = max(stamps) + MAX_SEGMENT_SEC / 2
            return stamps, duration

    return list(_FALLBACK_BOUNDARIES), duration or _FALLBACK_DURATION


def plan_segments(
    reference,
    *,
    min_sec: float = MIN_SEGMENT_SEC,
    max_sec: float = MAX_SEGMENT_SEC,
    max_count: int = MAX_SEGMENTS,
) -> List[Segment]:
    """Build the cut list for a `content_references` row."""
    meta = dict(getattr(reference, "metadata_json", None) or {})
    boundaries, duration = _reference_boundaries(meta)
    frame_records = meta.get("frame_records") or []

    intervals = compute_intervals(
        boundaries, duration, min_sec=min_sec, max_sec=max_sec, max_count=max_count
    )
    segments = [
        Segment(
            idx=i + 1,
            start_sec=start,
            end_sec=end,
            frame_s3_key=_nearest_frame_key(frame_records, start, end),
        )
        for i, (start, end) in enumerate(intervals)
    ]
    logger.info(
        "segment_plan_built",
        reference_id=str(getattr(reference, "id", "")),
        segments=len(segments),
        duration=duration,
        boundaries=len(boundaries),
    )
    return segments


def plan_to_json(
    segments: List[Segment],
    *,
    reference,
    fit_mode: str = "cover",
    source_audio_mode: str = "keep",
) -> Dict[str, Any]:
    meta = dict(getattr(reference, "metadata_json", None) or {})
    _, duration = _reference_boundaries(meta)
    return {
        "source_reference_id": str(getattr(reference, "id", "")),
        "source_media_s3_key": getattr(reference, "media_s3_key", None),
        "source_duration_sec": round(duration, 3),
        "fit_mode": fit_mode if fit_mode in FIT_MODES else "cover",
        "source_audio_mode": (
            source_audio_mode if source_audio_mode in SOURCE_AUDIO_MODES else "keep"
        ),
        "segments": [s.to_json() for s in segments],
    }


def plan_from_json(payload: Optional[dict]) -> List[Segment]:
    """Rehydrate segments from a stored plan. Malformed entries are
    skipped rather than raising — a hand-edited plan should degrade, not
    break the pipeline."""
    if not payload:
        return []
    out: list[Segment] = []
    for raw in payload.get("segments") or []:
        try:
            start = float(raw["start_sec"])
            end = float(raw["end_sec"])
            idx = int(raw["idx"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        action = raw.get("action") or "keep"
        out.append(
            Segment(
                idx=idx,
                start_sec=start,
                end_sec=end,
                frame_s3_key=raw.get("frame_s3_key"),
                action=action if action in ACTIONS else "keep",
                replace_prompt=raw.get("replace_prompt"),
                replace_asset_id=raw.get("replace_asset_id"),
                match_reason=raw.get("match_reason"),
            )
        )
    out.sort(key=lambda s: s.idx)
    return out


def source_ratio(plan: Optional[dict]) -> float:
    """Fraction of the output runtime that is verbatim source footage.

    Surfaced in the panel so the operator can see at a glance how much
    of the competitor's video they are about to republish.
    """
    segments = plan_from_json(plan)
    total = sum(s.duration_sec for s in segments if s.action != "drop")
    if total <= 0:
        return 0.0
    kept = sum(s.duration_sec for s in segments if s.action == "keep")
    return round(kept / total, 3)


def total_duration_sec(plan: Optional[dict]) -> float:
    return round(
        sum(s.duration_sec for s in plan_from_json(plan) if s.action != "drop"), 3
    )


def apply_plan_update(plan: Optional[dict], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge an admin edit into the stored plan.

    Only the fields present in `patch` change; segments are matched by
    `idx`. Boundaries are re-sorted and clamped so a hand-edit can't
    produce a zero-length or inverted window (which would make ffmpeg
    emit a 0-byte file at cut time).
    """
    merged = dict(plan or {})
    if patch.get("fit_mode") in FIT_MODES:
        merged["fit_mode"] = patch["fit_mode"]
    if patch.get("source_audio_mode") in SOURCE_AUDIO_MODES:
        merged["source_audio_mode"] = patch["source_audio_mode"]

    updates = {p["idx"]: p for p in (patch.get("segments") or []) if "idx" in p}
    if not updates:
        return merged

    out: list[dict] = []
    for seg in merged.get("segments") or []:
        upd = updates.get(seg.get("idx"))
        if not upd:
            out.append(seg)
            continue
        seg = dict(seg)
        for key in ("start_sec", "end_sec", "action", "replace_prompt", "replace_asset_id"):
            if upd.get(key) is not None:
                seg[key] = upd[key]
        start, end = float(seg["start_sec"]), float(seg["end_sec"])
        if end <= start:
            # Reject the edit rather than persist an impossible window.
            raise ValueError(
                f"segment {seg.get('idx')}: end_sec ({end}) must be greater than "
                f"start_sec ({start})"
            )
        seg["duration_sec"] = round(end - start, 3)
        out.append(seg)

    merged["segments"] = sorted(out, key=lambda s: s.get("idx", 0))
    return merged


# ---------------------------------------------------------------------------
# fan-out — materialize renders, stamp windows, enqueue the cut jobs
# ---------------------------------------------------------------------------


def start_segment_cuts(session, scenario) -> Dict[str, Any]:
    """Kick the cut fan-out for a repurpose scenario.

    One `segment_cut` job per aspect group (a single ffmpeg process cuts
    every keep segment for that aspect in one decode pass), plus one
    `image_gen` job per `replace` cell — those fall through to the
    existing synthesis path, seeded by the `init_image_s3_key` that
    `materialize_for_scenario` already stamps from the reference frames.
    """
    from app.services import queue as queue_svc
    from app.services import scene_renders as renders_svc

    renders_svc.materialize_for_scenario(session, scenario)
    renders = renders_svc.list_for_scenario(session, scenario.id)

    by_idx = {s.idx: s for s in plan_from_json(scenario.segment_plan)}
    aspects: set[str] = set()
    replace_render_ids: list[str] = []

    for render in renders:
        segment = by_idx.get(render.scene_idx)
        if segment is None:
            continue
        render.source_start_sec = segment.start_sec
        render.source_end_sec = segment.end_sec
        render.segment_action = segment.action
        render.match_reason = segment.match_reason
        session.add(render)
        if segment.action == "keep":
            aspects.add(render.aspect_ratio)
        elif segment.action == "replace":
            replace_render_ids.append(str(render.id))
    session.flush()

    enqueued_cuts = 0
    for aspect in sorted(aspects):
        try:
            queue_svc.enqueue(
                "segment_cut",
                "app.workers.segment_cut.run",
                str(scenario.id),
                aspect,
            )
            enqueued_cuts += 1
        except Exception as exc:  # noqa: BLE001 — Redis down shouldn't 500 the request
            logger.warning(
                "segment_cut_enqueue_failed",
                scenario_id=str(scenario.id),
                aspect=aspect,
                error=str(exc),
            )

    enqueued_replacements = 0
    for render_id in replace_render_ids:
        try:
            queue_svc.enqueue("image_gen", "app.workers.image_gen.run", render_id)
            enqueued_replacements += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "segment_replace_enqueue_failed", render_id=render_id, error=str(exc)
            )

    return {
        "aspects": sorted(aspects),
        "enqueued_cuts": enqueued_cuts,
        "enqueued_replacements": enqueued_replacements,
        "renders": len(renders),
    }
