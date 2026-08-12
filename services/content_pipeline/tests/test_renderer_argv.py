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


# ---------------------------------------------------------------------------
# CP video-quality pass — on-screen text, ducking, xfade
# ---------------------------------------------------------------------------

from app.services.renderer import SceneText, _escape_drawtext  # noqa: E402


def _fc(cmd):
    """Extract the -filter_complex payload from an argv list."""
    return cmd[cmd.index("-filter_complex") + 1]


def test_video_pipeline_applies_windowed_drawtext():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_video_keys=["a.mp4", "b.mp4"],
            scene_texts=[
                SceneText(text="hook line", style="bold_white", start_sec=0.0, end_sec=3.0, scene_pos=0),
            ],
        ),
        preset_key="ig_reels",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/concat.txt",
    )
    fc = _fc(cmd)
    assert "drawtext=" in fc
    assert "between(t,0.000,3.000)" in fc
    assert "hook line" in fc


def test_video_pipeline_no_drawtext_without_texts():
    cmd = build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"]),
        preset_key="ig_reels",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/concat.txt",
    )
    assert "drawtext=" not in _fc(cmd)


def test_ducking_replaces_plain_mix_when_vo_and_music():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_video_keys=["a.mp4"], voiceover_key="vo.mp3", music_key="m.mp3"
        ),
        preset_key="ig_reels",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/concat.txt",
    )
    fc = _fc(cmd)
    assert "sidechaincompress" in fc
    assert "loudnorm" in fc


def test_slideshow_attaches_per_scene_drawtext_unwindowed():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_image_keys=["a.jpg", "b.jpg"],
            scene_durations_sec=[3.0, 3.0],
            scene_texts=[SceneText(text="slide two", scene_pos=1)],
        ),
        preset_key="ig_feed_45",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/ignored.txt",
    )
    fc = _fc(cmd)
    assert "drawtext=" in fc
    assert "between(" not in fc  # per-chain text is not time-windowed
    assert "slide two" in fc


def test_slideshow_uses_xfade_when_fade_requested():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_image_keys=["a.jpg", "b.jpg", "c.jpg"],
            scene_durations_sec=[3.0, 3.0, 3.0],
            scene_transitions=["fade", "fade", "fade"],
        ),
        preset_key="ig_feed_45",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/ignored.txt",
    )
    fc = _fc(cmd)
    assert "xfade=transition=fade" in fc
    # offset math: first boundary at 3.0 - 0.35 = 2.65
    assert "offset=2.650" in fc


def test_slideshow_keeps_concat_when_all_cuts():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_image_keys=["a.jpg", "b.jpg"],
            scene_durations_sec=[3.0, 3.0],
            scene_transitions=["cut", "cut"],
        ),
        preset_key="ig_feed_45",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/ignored.txt",
    )
    fc = _fc(cmd)
    assert "xfade" not in fc
    assert "concat=n=2" in fc


def test_drawtext_escaping():
    assert _escape_drawtext("50% off: now") == "50\\% off\\: now"
    assert "\\\\" in _escape_drawtext("a\\b")


# ---------------------------------------------------------------------------
# Scene-aligned voiceover, outro, kinetic typography
# ---------------------------------------------------------------------------

from app.services.renderer import SceneVoiceover, build_kinetic_ass, _scene_offsets  # noqa: E402


def test_scene_voiceovers_build_adelay_bus_video_pipeline():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_video_keys=["a.mp4", "b.mp4"],
            scene_durations_sec=[3.0, 4.0],
            scene_voiceovers=[
                SceneVoiceover(s3_key="vo0.mp3", scene_pos=0),
                SceneVoiceover(s3_key="vo1.mp3", scene_pos=1),
            ],
            music_key="m.mp3",
        ),
        preset_key="ig_reels",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/c.txt",
    )
    fc = _fc(cmd)
    assert "adelay=0:all=1" in fc          # scene 0 at t=0
    assert "adelay=3000:all=1" in fc       # scene 1 at t=3s
    assert "[vobus]" in fc
    assert "sidechaincompress" in fc       # ducking still applies to the bus


def test_scene_voiceover_offsets_fade_compensated():
    # 3 scenes of 3s with fades: scene 2 starts at 6 - 2*0.35 = 5.3
    offsets = _scene_offsets([3.0, 3.0, 3.0], [0, 1, 2], fade=0.35)
    assert offsets == [0.0, 2.65, 5.3]


def test_kinetic_ass_word_events():
    ass = build_kinetic_ass(
        [SceneText(text="STOP scrolling now", style="kinetic_typography", scene_pos=0)],
        offsets_sec=[0.0],
        durations_sec=[3.0],
        width=1080,
        height=1920,
    )
    assert ass.count("Dialogue:") == 3            # one event per word
    assert "0:00:01.00" in ass                    # word 2 starts at 1.0s
    assert "fscx130" in ass                       # pop transform present


def test_kinetic_texts_skip_drawtext_and_use_subtitles():
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_image_keys=["a.jpg"],
            scene_durations_sec=[3.0],
            scene_texts=[SceneText(text="POP", style="kinetic_typography", scene_pos=0)],
        ),
        preset_key="ig_feed_45",
        output_path="/tmp/out.mp4",
        concat_list_path="/tmp/x.txt",
        kinetic_ass_path="kinetic.ass",
    )
    fc = _fc(cmd)
    assert "drawtext" not in fc
    assert "subtitles=kinetic.ass" in fc


def test_kinetic_ass_empty_for_non_kinetic_styles():
    assert build_kinetic_ass(
        [SceneText(text="hello", style="bold_white", scene_pos=0)],
        offsets_sec=[0.0], durations_sec=[3.0], width=1080, height=1920,
    ) == ""
