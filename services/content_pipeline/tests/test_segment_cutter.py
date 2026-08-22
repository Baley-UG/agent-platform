"""ffmpeg argv shape for the repurpose segment cutter.

Pure-argv tests, same style as `test_renderer_argv.py` — no ffmpeg
binary required.
"""

from __future__ import annotations

import pytest

from app.services import segment_cutter as cutter
from app.services.segment_cutter import Cut


def _segments(*windows):
    return [
        Cut(idx=i + 1, start_sec=start, end_sec=end)
        for i, (start, end) in enumerate(windows)
    ]


def _cmd(*windows, aspect="9:16", **kwargs):
    segs = _segments(*windows)
    return cutter.build_multicut_command(
        src_path="/tmp/source.mp4",
        segments=segs,
        aspect=aspect,
        out_paths=[f"/tmp/seg-{s.idx:02d}.mp4" for s in segs],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# normalize_filter
# ---------------------------------------------------------------------------


def test_cover_crops_to_fill_rather_than_letterboxing():
    f = cutter.normalize_filter("9:16")
    assert "force_original_aspect_ratio=increase" in f
    assert "crop=1080:1920" in f
    assert "pad=" not in f


def test_contain_letterboxes():
    f = cutter.normalize_filter("9:16", fit_mode="contain")
    assert "force_original_aspect_ratio=decrease" in f
    assert "pad=1080:1920" in f
    assert "crop=" not in f


def test_filter_resets_timebase_and_pts_for_concat():
    f = cutter.normalize_filter("9:16")
    assert "settb=AVTB" in f
    assert "setpts=PTS-STARTPTS" in f
    assert "setsar=1" in f


def test_aspect_dimensions_follow_presets():
    assert "1080:1350" in cutter.normalize_filter("4:5")
    assert "1080:1080" in cutter.normalize_filter("1:1")


def test_unknown_aspect_falls_back_to_vertical():
    assert "1080:1920" in cutter.normalize_filter("21:9")


def test_audio_is_normalized_for_concat():
    f = cutter.normalize_audio_filter()
    assert "aresample=48000" in f
    assert "channel_layouts=stereo" in f


# ---------------------------------------------------------------------------
# build_multicut_command
# ---------------------------------------------------------------------------


def test_single_input_many_outputs():
    """One decode pass, N outputs — cutting per-job would re-download
    and re-decode the same mp4 once per segment."""
    cmd = _cmd((0.0, 2.4), (2.4, 4.85), (4.85, 8.0))
    assert cmd.count("-i") == 1
    assert len([a for a in cmd if a.endswith(".mp4") and "seg-" in a]) == 3


def test_seek_is_output_side_for_frame_accuracy():
    # `-ss` must appear AFTER the input so ffmpeg decodes from 0 and
    # cuts exactly; input-side fast seek lands on a keyframe and drifts.
    cmd = _cmd((1.5, 4.0))
    assert cmd.index("-i") < cmd.index("-ss")


def test_seek_uses_start_and_duration_not_end():
    cmd = _cmd((2.4, 4.9))
    assert cmd[cmd.index("-ss") + 1] == "2.400"
    assert cmd[cmd.index("-t") + 1] == "2.500"


def test_each_segment_gets_its_own_seek_pair():
    cmd = _cmd((0.0, 2.0), (2.0, 5.0))
    assert cmd.count("-ss") == 2
    assert cmd.count("-t") == 2


def test_source_audio_is_re_encoded_not_dropped():
    """Repurpose ships the source's own (trending) audio — compose
    decides later whether to duck or drop it."""
    cmd = _cmd((0.0, 2.0))
    assert "-an" not in cmd
    assert "aac" in cmd


def test_output_is_faststart_h264():
    cmd = _cmd((0.0, 2.0))
    assert "libx264" in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "+faststart" in cmd


def test_fit_mode_reaches_the_filter():
    assert "pad=" in " ".join(_cmd((0.0, 2.0), fit_mode="contain"))
    assert "crop=" in " ".join(_cmd((0.0, 2.0), fit_mode="cover"))


def test_fps_override_reaches_the_filter():
    assert "fps=24" in " ".join(_cmd((0.0, 2.0), fps=24))


def test_length_mismatch_is_rejected():
    with pytest.raises(cutter.SegmentCutError, match="length mismatch"):
        cutter.build_multicut_command(
            src_path="/tmp/s.mp4",
            segments=_segments((0.0, 2.0), (2.0, 4.0)),
            aspect="9:16",
            out_paths=["/tmp/only-one.mp4"],
        )


def test_empty_segment_list_is_rejected():
    with pytest.raises(cutter.SegmentCutError, match="no segments"):
        cutter.build_multicut_command(
            src_path="/tmp/s.mp4", segments=[], aspect="9:16", out_paths=[]
        )


# ---------------------------------------------------------------------------
# key shape
# ---------------------------------------------------------------------------


def test_segment_key_is_project_namespaced_and_aspect_tagged():
    key = cutter.s3_key_for_segment("proj-1", "scen-1", 3, "9:16")
    assert key.startswith("projects/proj-1/scenes/")
    assert key.endswith("scen-1-segment-03-9x16.mp4")


def test_segment_keys_differ_per_aspect():
    a = cutter.s3_key_for_segment("p", "s", 1, "9:16")
    b = cutter.s3_key_for_segment("p", "s", 1, "4:5")
    assert a != b


def test_every_cut_gets_a_fresh_key():
    """A re-cut writes a new media_assets version; reusing the key would
    overwrite the bytes the prior version points at, so rollback would
    silently serve the replacement."""
    a = cutter.s3_key_for_segment("p", "s", 1, "9:16")
    b = cutter.s3_key_for_segment("p", "s", 1, "9:16")
    assert a != b


def test_segment_key_honours_the_shared_bucket_root_prefix(monkeypatch):
    from app.core import s3 as s3lib

    monkeypatch.setattr(s3lib.settings, "S3_ROOT_PREFIX", "agent_platform", raising=False)
    key = cutter.s3_key_for_segment("proj-1", "scen-1", 1, "9:16")
    assert key.startswith("agent_platform/projects/proj-1/scenes/")


# ---------------------------------------------------------------------------
# silent-source audio guarantee (concat needs a uniform stream layout)
# ---------------------------------------------------------------------------


def test_source_with_audio_maps_source_track():
    cmd = _cmd((0.0, 2.0))  # default src_has_audio=True
    assert "0:a:0" in cmd
    assert "anullsrc" not in " ".join(cmd)


def test_silent_source_synthesizes_a_track():
    """A source with no audio must still yield an aac stereo clip, or the
    concat demuxer breaks on a mixed-audio deck."""
    segs = _segments((0.0, 2.0), (2.0, 4.0))
    cmd = cutter.build_multicut_command(
        src_path="/tmp/s.mp4", segments=segs, aspect="9:16",
        out_paths=[f"/tmp/{s.idx}.mp4" for s in segs], src_has_audio=False,
    )
    joined = " ".join(cmd)
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in joined
    assert "1:a:0" in cmd          # every output maps the silent track
    assert cmd.count("-shortest") == 2   # trimmed to each segment
    assert "aac" in cmd            # still encodes an audio codec
