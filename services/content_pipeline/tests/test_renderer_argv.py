"""ffmpeg arg builder — pure shape assertions, no subprocess."""

from app.services.renderer import ComposeInputs, build_compose_command, write_concat_list
from pathlib import Path


def _common(out="/tmp/out.mp4", concat="/tmp/cat.txt"):
    return {"output_path": out, "concat_list_path": concat}


def test_argv_starts_with_ffmpeg_and_overwrite_flag():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["projects/p/scenes/a.mp4"]),
        preset_key="ig_reels",
        **_common(),
    )
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd
    assert "-hide_banner" in cmd


def test_argv_uses_concat_demuxer_for_scenes():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["projects/p/scenes/a.mp4", "projects/p/scenes/b.mp4"]),
        preset_key="ig_reels",
        **_common(),
    )
    # -f concat -safe 0 -i <list>
    assert cmd[cmd.index("-f") + 1] == "concat"
    assert "-safe" in cmd
    list_idx = cmd.index("-i")
    assert cmd[list_idx + 1] == _common()["concat_list_path"]


def test_argv_includes_voiceover_input_when_present():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"], voiceover_key="audio/v.mp3"),
        preset_key="ig_reels",
        **_common(),
    )
    # Voiceover gets a -i input.
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert any(p.endswith("v.mp3") for p in inputs)


def test_argv_includes_music_input_when_present():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"], music_key="music/track.mp3"),
        preset_key="ig_reels",
        **_common(),
    )
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert any(p.endswith("track.mp3") for p in inputs)


def test_argv_emits_amix_when_voiceover_and_music_both_present():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_video_keys=["a.mp4"], voiceover_key="audio/v.mp3", music_key="music/m.mp3"
        ),
        preset_key="ig_reels",
        **_common(),
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" in fc
    assert "loudnorm" in fc


def test_argv_skips_amix_when_only_voiceover():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"], voiceover_key="audio/v.mp3"),
        preset_key="ig_reels",
        **_common(),
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" not in fc
    assert "loudnorm" in fc


def test_argv_dimensions_match_preset():
    cmd_reels = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"]),
        preset_key="ig_reels",
        **_common(),
    )
    fc_reels = cmd_reels[cmd_reels.index("-filter_complex") + 1]
    assert "scale=1080:1920" in fc_reels

    cmd_feed = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"]),
        preset_key="ig_feed_45",
        **_common(),
    )
    fc_feed = cmd_feed[cmd_feed.index("-filter_complex") + 1]
    assert "scale=1080:1350" in fc_feed


def test_argv_encodes_to_h264_aac_faststart():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"], voiceover_key="v.mp3"),
        preset_key="ig_reels",
        **_common(),
    )
    assert "libx264" in cmd
    assert "aac" in cmd
    assert "+faststart" in cmd


def test_concat_list_writer(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    list_path = write_concat_list(tmp_path, ["projects/p/scenes/a.mp4", "projects/p/scenes/b.mp4"])
    text = list_path.read_text()
    assert "file '" in text
    assert text.count("file '") == 2
    # Each line points at <tmp_path>/in/<basename>
    assert "/in/a.mp4'" in text
    assert "/in/b.mp4'" in text
