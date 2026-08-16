"""Source-audio routing in compose (`source_audio_mode`).

Repurpose keeps the source's own — usually trending — audio, which is
new: every prior pipeline discarded it. The regression guard that
matters most is that `drop` (the default) leaves existing recipes'
argv byte-identical.
"""

from __future__ import annotations

import pytest

from app.services.renderer import ComposeInputs, build_compose_command

COMMON = {"output_path": "/tmp/out.mp4", "concat_list_path": "/tmp/cat.txt"}


def _cmd(**kwargs):
    return build_compose_command(
        inputs=ComposeInputs(scene_video_keys=["a.mp4"], **kwargs),
        preset_key="ig_reels",
        **COMMON,
    )


def _graph(cmd) -> str:
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


# ---------------------------------------------------------------------------
# drop — the default, and the byte-identical regression guard
# ---------------------------------------------------------------------------


def test_default_mode_is_drop():
    assert ComposeInputs().source_audio_mode == "drop"


def test_drop_argv_is_unchanged_from_before_repurpose():
    """Explicit `drop` and the default must produce the same argv, and
    that argv must not reference the concat input's audio stream."""
    default = _cmd()
    explicit = _cmd(source_audio_mode="drop")
    assert default == explicit
    assert "[0:a]" not in _graph(default)


def test_drop_still_maps_the_silent_input():
    cmd = _cmd(source_audio_mode="drop")
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in cmd


def test_drop_with_music_keeps_the_music_only_graph():
    graph = _graph(_cmd(music_key="m.mp3", source_audio_mode="drop"))
    assert "[0:a]" not in graph
    assert "loudnorm" in graph


# ---------------------------------------------------------------------------
# keep — the repurpose default
# ---------------------------------------------------------------------------


def test_keep_maps_the_concat_audio_stream():
    graph = _graph(_cmd(source_audio_mode="keep"))
    assert "[0:a]" in graph
    assert "[aout]" in graph


def test_keep_does_not_declare_a_silent_input():
    """A silent bed alongside real audio would be dead weight — and
    `-shortest` could pick the wrong stream to trim against."""
    cmd = _cmd(source_audio_mode="keep")
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" not in cmd


def test_keep_without_voiceover_ships_source_at_full_level():
    graph = _graph(_cmd(source_audio_mode="keep"))
    assert "[0:a]volume=1.0" in graph


def test_keep_normalizes_loudness():
    assert "loudnorm" in _graph(_cmd(source_audio_mode="keep"))


def test_keep_mixes_music_under_the_source():
    graph = _graph(_cmd(source_audio_mode="keep", music_key="m.mp3"))
    assert "amix=inputs=2" in graph
    assert "normalize=0" in graph


# ---------------------------------------------------------------------------
# duck — source audio under our voiceover
# ---------------------------------------------------------------------------


def test_duck_routes_source_through_the_sidechain_bed():
    graph = _graph(_cmd(source_audio_mode="duck", voiceover_key="v.mp3"))
    assert "sidechaincompress" in graph
    assert "[0:a]" in graph


def test_duck_lowers_the_source_under_speech():
    graph = _graph(_cmd(source_audio_mode="duck", voiceover_key="v.mp3"))
    assert "[0:a]volume=0.3" in graph


def test_source_volume_is_overridable():
    graph = _graph(
        _cmd(source_audio_mode="duck", voiceover_key="v.mp3", source_audio_volume=0.15)
    )
    assert "[0:a]volume=0.15" in graph


def test_duck_with_music_folds_both_beds_into_one_sidechain():
    graph = _graph(
        _cmd(source_audio_mode="duck", voiceover_key="v.mp3", music_key="m.mp3")
    )
    assert "amix=inputs=2" in graph  # source + music → bedbus
    assert "sidechaincompress" in graph
    # Pre-mixed beds must not be re-attenuated by the ducking filter.
    assert "[bedbus]volume=1.0[bgpre]" in graph


def test_keep_with_voiceover_also_ducks():
    """`keep` + narration can't mean "both at full level" — that is
    unlistenable. The bed drops under speech either way."""
    graph = _graph(_cmd(source_audio_mode="keep", voiceover_key="v.mp3"))
    assert "sidechaincompress" in graph


# ---------------------------------------------------------------------------
# slideshow path has no source audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["keep", "duck", "drop"])
def test_slideshow_ignores_source_audio_mode(mode):
    cmd = build_compose_command(
        inputs=ComposeInputs(
            scene_image_keys=["a.jpg", "b.jpg"],
            scene_durations_sec=[2.0, 2.0],
            source_audio_mode=mode,
        ),
        preset_key="ig_reels",
        **COMMON,
    )
    assert "[0:a]" not in _graph(cmd)
