"""Compose input derivation — caption windowing + duration source.

These pin the two blocker fixes: captions must be windowed to their
clip's slot on the concatenated timeline (not rendered all at once), and
the timeline must use the PROBED clip duration, not the planned window.
"""

from __future__ import annotations

import uuid

from app.models.remake_shots import RemakeShot
from app.services import remake_composer as comp


def _shot(idx, start, end, *, text=None, technique="copy", output_duration=None):
    return RemakeShot(
        remake_id=uuid.uuid4(), idx=idx, start_sec=start, end_sec=end,
        technique=technique, output_duration_sec=output_duration,
        text_plan=[{"replacement": text}] if text else None,
    )


# ---------------------------------------------------------------------------
# _clip_durations
# ---------------------------------------------------------------------------


def test_prefers_probed_output_duration():
    # planned window 2.0s but the re-encoded clip measured 2.07s.
    clips = [_shot(0, 0.0, 2.0, output_duration=2.07)]
    assert comp._clip_durations(clips) == [2.07]


def test_falls_back_to_planned_window_when_unprobed():
    clips = [_shot(0, 1.0, 3.4, output_duration=None)]
    assert comp._clip_durations(clips)[0] == 2.4


def test_uses_trim_window_when_present_and_unprobed():
    s = _shot(0, 0.0, 8.0, output_duration=None)
    s.trim_start_sec = 2.0
    s.trim_end_sec = 5.0
    assert comp._clip_durations([s])[0] == 3.0


# ---------------------------------------------------------------------------
# _scene_texts — the BLOCKER fix
# ---------------------------------------------------------------------------


def test_captions_are_windowed_to_cumulative_slots():
    clips = [
        _shot(0, 0.0, 2.0, text="one", output_duration=2.0),
        _shot(1, 2.0, 5.0, text="two", output_duration=3.0),
        _shot(2, 5.0, 6.0, text="three", output_duration=1.0),
    ]
    durs = comp._clip_durations(clips)
    texts = comp._scene_texts(clips, durs)
    assert [(t.text, t.start_sec, t.end_sec) for t in texts] == [
        ("one", 0.0, 2.0),
        ("two", 2.0, 5.0),
        ("three", 5.0, 6.0),
    ]
    # Every caption has a real, non-empty window (the bug was start==end==0
    # → the renderer emits no `enable=between`, so all render at once).
    assert all(t.end_sec > t.start_sec for t in texts)


def test_shots_without_text_still_advance_the_clock():
    clips = [
        _shot(0, 0.0, 2.0, text=None, output_duration=2.0),   # no caption
        _shot(1, 2.0, 4.0, text="late", output_duration=2.0),
    ]
    durs = comp._clip_durations(clips)
    texts = comp._scene_texts(clips, durs)
    assert len(texts) == 1
    # The captioned second clip is windowed AFTER the silent first clip.
    assert texts[0].start_sec == 2.0 and texts[0].end_sec == 4.0


def test_empty_text_plan_produces_no_caption():
    clips = [_shot(0, 0.0, 2.0, text="   ", output_duration=2.0)]
    assert comp._scene_texts(clips, comp._clip_durations(clips)) == []
