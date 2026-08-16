"""Segment planning — the pure interval math behind repurpose mode."""

from __future__ import annotations

import pytest

from app.services import segments as svc


class FakeReference:
    """Minimal stand-in for a `content_references` row."""

    def __init__(self, metadata: dict | None = None, media_s3_key: str | None = None):
        self.id = "11111111-1111-1111-1111-111111111111"
        self.metadata_json = metadata or {}
        self.media_s3_key = media_s3_key


# ---------------------------------------------------------------------------
# compute_intervals
# ---------------------------------------------------------------------------


def test_boundaries_become_contiguous_windows():
    intervals = svc.compute_intervals([3.0, 6.0], 9.0)
    assert intervals == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]


def test_windows_cover_the_whole_duration_with_no_gaps():
    intervals = svc.compute_intervals([2.0, 5.5, 8.0], 12.0)
    assert intervals[0][0] == 0.0
    assert intervals[-1][1] == 12.0
    for prev, cur in zip(intervals, intervals[1:]):
        assert prev[1] == cur[0]


def test_short_intervals_merge_into_predecessor():
    # 0.3s and 0.4s gaps are below MIN_SEGMENT_SEC — a burst of rapid
    # cuts should collapse into one usable shot, not three glitches.
    intervals = svc.compute_intervals([0.3, 0.7, 4.0], 8.0)
    assert all(end - start >= svc.MIN_SEGMENT_SEC for start, end in intervals)


def test_short_tail_merges_backwards():
    # The final window would be 0.2s; it must merge into its predecessor
    # rather than survive as a stub.
    intervals = svc.compute_intervals([4.0], 4.2)
    assert len(intervals) == 1
    assert intervals[0] == (0.0, 4.2)


def test_long_intervals_split_evenly():
    intervals = svc.compute_intervals([], 12.0, max_sec=5.0)
    assert len(intervals) == 3
    assert all(end - start <= 5.0 + 1e-6 for start, end in intervals)


def test_count_is_capped():
    boundaries = [float(i) * 2 for i in range(1, 30)]
    intervals = svc.compute_intervals(boundaries, 60.0, max_count=6)
    assert len(intervals) <= 6
    # Capping must not lose coverage.
    assert intervals[0][0] == 0.0
    assert intervals[-1][1] == 60.0


def test_zero_duration_yields_nothing():
    assert svc.compute_intervals([1.0, 2.0], 0.0) == []


def test_boundaries_outside_duration_are_ignored():
    intervals = svc.compute_intervals([-1.0, 3.0, 99.0], 6.0)
    assert intervals == [(0.0, 3.0), (3.0, 6.0)]


def test_duplicate_boundaries_collapse():
    intervals = svc.compute_intervals([3.0, 3.0, 3.0], 6.0)
    assert intervals == [(0.0, 3.0), (3.0, 6.0)]


# ---------------------------------------------------------------------------
# fallback ladder
# ---------------------------------------------------------------------------


def test_prefers_scene_boundaries_when_present():
    ref = FakeReference(
        {"scene_boundaries_sec": [2.0, 5.0], "duration_sec_probed": 8.0}
    )
    segs = svc.plan_segments(ref)
    assert [ (s.start_sec, s.end_sec) for s in segs ] == [(0.0, 2.0), (2.0, 5.0), (5.0, 8.0)]


def test_falls_back_to_frame_record_timestamps():
    # References imported before repurpose existed have no
    # `scene_boundaries_sec` — the thumbnail timestamps are a worse cut
    # list, but better than fixed intervals.
    ref = FakeReference(
        {
            "duration_sec_probed": 9.0,
            "frame_records": [
                {"s3_key": "a.jpg", "timestamp_sec": 3.0},
                {"s3_key": "b.jpg", "timestamp_sec": 6.0},
            ],
        }
    )
    segs = svc.plan_segments(ref)
    assert len(segs) == 3
    assert segs[0].start_sec == 0.0


def test_falls_back_to_fixed_intervals_when_metadata_is_empty():
    segs = svc.plan_segments(FakeReference({}))
    assert segs, "an empty reference must still yield a usable plan"
    assert segs[0].start_sec == 0.0
    assert segs[-1].end_sec == svc._FALLBACK_DURATION


def test_segments_are_1_based_and_sequential():
    ref = FakeReference({"scene_boundaries_sec": [2.0, 4.0], "duration_sec_probed": 6.0})
    segs = svc.plan_segments(ref)
    assert [s.idx for s in segs] == [1, 2, 3]


def test_default_action_is_keep():
    """Birebir kes-yapıştır: nothing is replaced unless the analyzer
    (or an admin) says so."""
    ref = FakeReference({"scene_boundaries_sec": [2.0], "duration_sec_probed": 5.0})
    assert all(s.action == "keep" for s in svc.plan_segments(ref))


def test_nearest_frame_is_attached_to_each_segment():
    ref = FakeReference(
        {
            "scene_boundaries_sec": [4.0],
            "duration_sec_probed": 8.0,
            "frame_records": [
                {"s3_key": "early.jpg", "timestamp_sec": 1.0},
                {"s3_key": "late.jpg", "timestamp_sec": 7.0},
            ],
        }
    )
    segs = svc.plan_segments(ref)
    assert segs[0].frame_s3_key == "early.jpg"
    assert segs[1].frame_s3_key == "late.jpg"


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_plan_json_round_trip():
    ref = FakeReference(
        {"scene_boundaries_sec": [2.0, 5.0], "duration_sec_probed": 8.0},
        media_s3_key="projects/p/references/x.mp4",
    )
    segs = svc.plan_segments(ref)
    payload = svc.plan_to_json(segs, reference=ref)
    assert payload["source_media_s3_key"] == "projects/p/references/x.mp4"
    assert payload["source_audio_mode"] == "keep"
    assert payload["fit_mode"] == "cover"

    restored = svc.plan_from_json(payload)
    assert [(s.idx, s.start_sec, s.end_sec) for s in restored] == [
        (s.idx, s.start_sec, s.end_sec) for s in segs
    ]


def test_plan_from_json_skips_malformed_entries():
    payload = {
        "segments": [
            {"idx": 1, "start_sec": 0.0, "end_sec": 2.0},
            {"idx": 2, "start_sec": 5.0, "end_sec": 4.0},  # inverted
            {"idx": 3, "start_sec": "x", "end_sec": 8.0},  # unparseable
            {"start_sec": 8.0, "end_sec": 9.0},  # no idx
        ]
    }
    assert [s.idx for s in svc.plan_from_json(payload)] == [1]


def test_plan_from_json_normalizes_unknown_action():
    payload = {"segments": [{"idx": 1, "start_sec": 0.0, "end_sec": 2.0, "action": "nonsense"}]}
    assert svc.plan_from_json(payload)[0].action == "keep"


def test_plan_from_json_of_none_is_empty():
    assert svc.plan_from_json(None) == []


# ---------------------------------------------------------------------------
# source_ratio — the "how much of this is theirs" gauge
# ---------------------------------------------------------------------------


def _plan(*entries):
    return {
        "segments": [
            {"idx": i + 1, "start_sec": 0.0, "end_sec": dur, "action": action}
            for i, (dur, action) in enumerate(entries)
        ]
    }


def test_source_ratio_all_keep_is_one():
    assert svc.source_ratio(_plan((2.0, "keep"), (3.0, "keep"))) == 1.0


def test_source_ratio_all_replace_is_zero():
    assert svc.source_ratio(_plan((2.0, "replace"), (3.0, "replace"))) == 0.0


def test_source_ratio_is_runtime_weighted_not_segment_counted():
    # One long kept shot outweighs one short replaced shot.
    assert svc.source_ratio(_plan((9.0, "keep"), (1.0, "replace"))) == 0.9


def test_dropped_segments_leave_the_denominator():
    plan = _plan((5.0, "keep"), (5.0, "drop"))
    assert svc.source_ratio(plan) == 1.0
    assert svc.total_duration_sec(plan) == 5.0


def test_source_ratio_of_empty_plan_is_zero():
    assert svc.source_ratio(None) == 0.0
    assert svc.source_ratio({"segments": []}) == 0.0


# ---------------------------------------------------------------------------
# apply_plan_update — admin edits
# ---------------------------------------------------------------------------


def test_patch_flips_action_without_touching_boundaries():
    plan = {"segments": [{"idx": 1, "start_sec": 0.0, "end_sec": 3.0, "action": "keep"}]}
    out = svc.apply_plan_update(plan, {"segments": [{"idx": 1, "action": "replace"}]})
    assert out["segments"][0]["action"] == "replace"
    assert out["segments"][0]["end_sec"] == 3.0


def test_patch_recomputes_duration_when_boundaries_move():
    plan = {"segments": [{"idx": 1, "start_sec": 0.0, "end_sec": 3.0, "duration_sec": 3.0}]}
    out = svc.apply_plan_update(plan, {"segments": [{"idx": 1, "end_sec": 5.0}]})
    assert out["segments"][0]["duration_sec"] == 5.0


def test_patch_rejects_inverted_window():
    plan = {"segments": [{"idx": 1, "start_sec": 2.0, "end_sec": 4.0}]}
    with pytest.raises(ValueError, match="greater than"):
        svc.apply_plan_update(plan, {"segments": [{"idx": 1, "end_sec": 1.0}]})


def test_patch_updates_top_level_modes():
    out = svc.apply_plan_update({}, {"fit_mode": "contain", "source_audio_mode": "duck"})
    assert out["fit_mode"] == "contain"
    assert out["source_audio_mode"] == "duck"


def test_patch_ignores_unknown_mode_values():
    out = svc.apply_plan_update({"fit_mode": "cover"}, {"fit_mode": "stretch"})
    assert out["fit_mode"] == "cover"


def test_patch_leaves_unlisted_segments_alone():
    plan = {
        "segments": [
            {"idx": 1, "start_sec": 0.0, "end_sec": 2.0, "action": "keep"},
            {"idx": 2, "start_sec": 2.0, "end_sec": 4.0, "action": "keep"},
        ]
    }
    out = svc.apply_plan_update(plan, {"segments": [{"idx": 2, "action": "drop"}]})
    assert out["segments"][0]["action"] == "keep"
    assert out["segments"][1]["action"] == "drop"
