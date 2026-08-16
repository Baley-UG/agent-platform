"""ffmpeg compose — build the final video for one render_variant.

CP-M5 ships a baseline that produces a working final video with audio.
Polish (per-scene xfade transitions, sidechain ducking, on-screen text
overlays at safe-zone-aware positions, outro template insertion) is
captured in PLAN § 11 and lands incrementally in CP-M5.5+.

Architecture:
- `build_compose_command(...)` returns a pure list of ffmpeg argv. Tests
  assert the shape without invoking ffmpeg.
- `compose_variant(...)` orchestrates: download inputs from S3 to a
  tmpdir, run ffmpeg, upload the output to S3, return metadata. Wraps
  subprocess so a missing ffmpeg binary surfaces as a clean exception.

Inputs we accept (all S3 keys):
- `scene_video_keys`: scene_renders' video_asset_id S3 keys, in scene order,
  filtered to the variant's aspect_group.
- `voiceover_key`: optional voiceover mp3.
- `music_key`: optional music mp3.

Compose plan (current baseline):
1. Concat all scene videos (concat demuxer; same dimensions assumed).
2. Mix voiceover + music with `amix` (voiceover louder; sidechain ducking
   later).
3. Loudness-normalize the mix (`loudnorm` filter).
4. Scale + pad to preset dimensions if needed (in practice the masters
   already match — the scale is a safety net).
5. Encode h264 + aac at the preset's fps.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.core import s3
from app.core.config import settings
from app.core.logging import logger
from app.services.presets import PRESETS, VariantPreset


class FFmpegError(RuntimeError):
    """Raised when ffmpeg fails or its binary is missing."""


@dataclass
class SceneText:
    """One scene's on-screen text, threaded from scenario_json.

    `start_sec`/`end_sec` window the drawtext on the CONCATENATED
    timeline (video pipeline). The slideshow pipeline ignores the window
    and attaches the text to its scene's own filter chain instead.
    """

    text: str
    style: str = "bold_white"  # analyzer's text_style vocabulary
    start_sec: float = 0.0
    end_sec: float = 0.0
    # 0-based position of the owning scene in render order. The
    # slideshow pipeline uses this to attach the drawtext to the right
    # per-scene filter chain (each chain's clock starts at 0, so the
    # start/end window doesn't apply there).
    scene_pos: int = 0


@dataclass
class SceneVoiceover:
    """One scene's TTS clip, mixed into the voice bus at its scene's
    start offset. Produced by the per-scene audio_gen path."""

    s3_key: str
    scene_pos: int  # 0-based position in render order


@dataclass
class ComposeInputs:
    """Compose inputs. EITHER `scene_video_keys` (reel/video) OR
    `scene_image_keys` (photo/carousel) — never both populated
    simultaneously. The renderer picks the appropriate ffmpeg pipeline.
    """

    # Reel/video path: per-scene 5-10s mp4 from Seedance i2v.
    scene_video_keys: List[str] = field(default_factory=list)
    # Photo/carousel path: per-scene jpg from fal Flux. Each is rendered
    # for the matching `scene_durations_sec[i]` (Ken Burns push-in by
    # default to keep the slideshow visually alive).
    scene_image_keys: List[str] = field(default_factory=list)
    # Required when `scene_image_keys` is populated — one duration per
    # image, in seconds.
    scene_durations_sec: List[float] = field(default_factory=list)
    # Per-scene on-screen text (from scenario_json scenes[].on_screen_text).
    # One entry per scene that HAS text; scenes without text are absent.
    # Applied via drawtext — slideshow attaches per-scene, video pipeline
    # windows by `start_sec`/`end_sec` on the concatenated stream.
    scene_texts: List[SceneText] = field(default_factory=list)
    # Per-boundary transitions for the slideshow pipeline, one entry per
    # scene (its transition OUT). Vocabulary: "cut" | "fade" | anything
    # else treated as fade. The video pipeline still hard-cuts (concat
    # demuxer can't xfade without a structural refactor — deferred).
    scene_transitions: List[str] = field(default_factory=list)
    voiceover_key: Optional[str] = None
    # Per-scene TTS clips (preferred over `voiceover_key` when present).
    # Each clip is delayed to its scene's start so speech lands exactly
    # on its scene instead of free-running over the whole video.
    scene_voiceovers: List[SceneVoiceover] = field(default_factory=list)
    music_key: Optional[str] = None
    # Appended after the main compose via a second normalization pass
    # (`append_outro`). Any codec/dims — the pass re-encodes.
    outro_video_key: Optional[str] = None
    # What to do with the audio the scene videos carry.
    #   "drop" — discard it (the CP-M5 behaviour, and still the default
    #            so recreate/brand_build argv is unchanged).
    #   "keep" — ship it as the output's audio. Repurpose uses this:
    #            the source's trending audio is a large part of why it
    #            performed, and re-voicing it costs reach.
    #   "duck" — mix it under our voiceover via the existing
    #            sidechaincompress bus.
    # Only meaningful on the concat (scene_video_keys) path; the
    # slideshow path has no source audio to begin with.
    source_audio_mode: str = "drop"
    source_audio_volume: float = 0.30


@dataclass
class ComposeRecipe:
    """Snapshot of compose decisions, persisted on render_variant.render_recipe."""

    preset_key: str
    width: int
    height: int
    fps: int
    audio_lufs: int
    container: str
    has_voiceover: bool
    has_music: bool
    music_volume: float = 0.25
    voiceover_volume: float = 1.0
    extra: dict = field(default_factory=dict)


def _preset(preset_key: str) -> VariantPreset:
    if preset_key not in PRESETS:
        raise FFmpegError(f"unknown preset: {preset_key}")
    return PRESETS[preset_key]


# ---------------------------------------------------------------------------
# On-screen text (drawtext)
# ---------------------------------------------------------------------------

# Bundled with the render image via `fonts-dejavu-core` (see
# Dockerfile.ffmpeg). DejaVu covers Latin + Turkish glyphs; brand-kit
# custom fonts land later (brand_kits.font_family → font file mapping).
_FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _escape_drawtext(text: str) -> str:
    r"""Escape a string for ffmpeg drawtext's `text=` option.

    drawtext parses `\` `'` `:` `%` specially INSIDE the filtergraph
    string. Order matters — backslash first.
    """
    out = text.replace("\\", "\\\\")
    out = out.replace("'", "\\\\\\'")  # quote inside quoted filter arg
    out = out.replace(":", "\\:")
    out = out.replace("%", "\\%")
    return out


# text_style vocabulary → drawtext parameter fragments. Sizes are
# expressed relative to the canvas height (h/K) so the same style scales
# across 9:16 / 4:5 / 1:1. Positions respect a rough safe-zone: TikTok
# and Reels chrome eats the bottom ~18% and top ~8%, so "bottom" text
# sits at 74% height and "top" at 12%.
_TEXT_STYLES = {
    "bold_white": {
        "fontcolor": "white",
        "borderw": "6",
        "bordercolor": "black@0.85",
        "fontsize": "h/16",
        "y": "h*0.74",
    },
    "subtle_caption": {
        "fontcolor": "white@0.92",
        "borderw": "3",
        "bordercolor": "black@0.6",
        "fontsize": "h/24",
        "y": "h*0.78",
    },
    "kinetic_typography": {
        # True kinetic (word-by-word pop) needs ASS subtitles — Phase
        # CP-M5.5+. Until then render as high-impact centered text.
        "fontcolor": "white",
        "borderw": "7",
        "bordercolor": "black@0.9",
        "fontsize": "h/12",
        "y": "(h-text_h)/2",
    },
    "handwritten": {
        # No handwritten font bundled yet; falls back to DejaVu with a
        # softer look.
        "fontcolor": "white@0.95",
        "borderw": "2",
        "bordercolor": "black@0.5",
        "fontsize": "h/18",
        "y": "h*0.72",
    },
}


def _drawtext_filter(st: SceneText, *, windowed: bool) -> str:
    """One drawtext filter string for a scene's on-screen text.

    `windowed=True` adds `enable='between(t,start,end)'` — used by the
    video pipeline where all scenes share one concatenated stream. The
    slideshow pipeline attaches the filter to the scene's own chain, so
    the window is unnecessary (and would be wrong — each chain's clock
    starts at 0).
    """
    style = _TEXT_STYLES.get(st.style, _TEXT_STYLES["bold_white"])
    parts = [
        f"fontfile={_FONT_FILE}",
        f"text='{_escape_drawtext(st.text)}'",
        f"fontsize={style['fontsize']}",
        f"fontcolor={style['fontcolor']}",
        f"borderw={style['borderw']}",
        f"bordercolor={style['bordercolor']}",
        "x=(w-text_w)/2",
        f"y={style['y']}",
    ]
    if windowed and st.end_sec > st.start_sec:
        parts.append(f"enable='between(t,{st.start_sec:.3f},{st.end_sec:.3f})'")
    return "drawtext=" + ":".join(parts)


# ---------------------------------------------------------------------------
# Audio ducking
# ---------------------------------------------------------------------------


def _slideshow_wants_fade(transitions: List[str], n_scenes: int) -> bool:
    """Single source of truth for whether the slideshow path will build
    an xfade chain (vs plain concat). Used both by the join logic and
    by kinetic-ASS timing so subtitle timestamps stay in sync with the
    fade-compressed timeline."""
    if n_scenes <= 1:
        return False
    return any(
        (transitions[i] if i < len(transitions) else "cut") not in ("cut", "", None)
        for i in range(n_scenes - 1)
    )


def _ass_ts(seconds: float) -> str:
    """ASS timestamp: H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_kinetic_ass(
    scene_texts: List["SceneText"],
    *,
    offsets_sec: List[float],
    durations_sec: List[float],
    width: int,
    height: int,
) -> str:
    """ASS subtitle document for kinetic_typography scenes.

    TikTok-style word-by-word pop: each word of the scene's
    `on_screen_text` gets its own Dialogue event, evenly spaced across
    the scene's duration, centered, with a quick scale-pop transform.
    Only scenes whose style is `kinetic_typography` contribute events —
    other styles stay on drawtext.

    `offsets_sec[i]` / `durations_sec[i]` are indexed by SceneText's
    position in the `scene_texts` list (the caller aligns them).
    Pure function — caller writes the returned string to a file and
    hands the path to `build_compose_command(kinetic_ass_path=…)`.
    """
    font_size = max(28, height // 12)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour,"
        " Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Kinetic,DejaVu Sans,{font_size},&H00FFFFFF,&H00000000,&H7F000000,"
        "-1,4,1,5,40,40,0\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Text\n"
    )
    events: List[str] = []
    for i, st in enumerate(scene_texts):
        if st.style != "kinetic_typography" or not st.text.strip():
            continue
        words = st.text.split()
        if not words:
            continue
        start = offsets_sec[i] if i < len(offsets_sec) else st.start_sec
        duration = durations_sec[i] if i < len(durations_sec) else max(0.5, st.end_sec - st.start_sec)
        per_word = duration / len(words)
        for w_idx, word in enumerate(words):
            w_start = start + w_idx * per_word
            w_end = w_start + per_word
            # Scale-pop: 130% → 100% over the first 120ms.
            fx = r"{\fscx130\fscy130\t(0,120,\fscx100\fscy100)\fad(40,30)}"
            safe_word = word.replace("{", "").replace("}", "").replace("\\", "")
            events.append(
                f"Dialogue: 0,{_ass_ts(w_start)},{_ass_ts(w_end)},Kinetic,{fx}{safe_word}"
            )
    if not events:
        return ""
    return header + "\n".join(events) + "\n"


def _scene_offsets(
    durations_sec: List[float], scene_positions: List[int], *, fade: float
) -> List[float]:
    """Start offset (seconds) of each requested scene position.

    `fade` compensates for xfade overlap in the slideshow pipeline —
    every boundary before a scene pulls its start earlier by one fade
    width. The reel pipeline passes fade=0 (hard-cut concat).

    Missing/short duration entries default to 3.0s, matching the
    slideshow fallback, so one malformed scene doesn't corrupt every
    later offset.
    """
    starts: List[float] = []
    elapsed = 0.0
    for d in durations_sec:
        starts.append(elapsed)
        try:
            dur = float(d) or 3.0
        except (TypeError, ValueError):
            dur = 3.0
        elapsed += dur
    out: List[float] = []
    for pos in scene_positions:
        base = starts[pos] if 0 <= pos < len(starts) else 0.0
        out.append(max(0.0, base - fade * pos))
    return out


def _scene_voice_bus(
    voice_input_indices: List[int], offsets_sec: List[float]
) -> List[str]:
    """Sum per-scene TTS clips into one voice bus, each delayed to its
    scene's start. Returns filter parts producing `[vobus]`.

    `adelay=…:all=1` delays every channel; `amix normalize=0` keeps each
    clip at full level (clips never overlap by design — scenes are
    sequential — so summing without normalization is safe and avoids the
    1/N volume drop amix applies by default).
    """
    parts: List[str] = []
    labels: List[str] = []
    for i, (idx, offset) in enumerate(zip(voice_input_indices, offsets_sec)):
        delay_ms = max(0, int(round(offset * 1000)))
        parts.append(f"[{idx}:a]adelay={delay_ms}:all=1[svo{i}]")
        labels.append(f"[svo{i}]")
    if len(labels) == 1:
        parts.append(f"{labels[0]}anull[vobus]")
    else:
        parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[vobus]"
        )
    return parts


def _ducked_audio_filters(
    recipe: "ComposeRecipe", vo_src: str, mu_src: str, mu_volume: Optional[float] = None
) -> List[str]:
    """Voiceover + background mix with sidechain ducking.

    `vo_src` / `mu_src` are filter labels (`[1:a]` for a raw input,
    `[vobus]` for the per-scene voice bus, `[bedbus]` for a pre-mixed
    source-audio + music bed). The bed is compressed WHENEVER the voice
    carries signal — drops ~12dB under speech, recovers in ~400ms of
    silence. This is the standard "podcast bed" treatment; fixed-volume
    `amix` made the music fight the voice.

    `mu_volume` overrides the bed gain; pass 1.0 when the caller already
    applied per-source volumes while pre-mixing.
    """
    bed_volume = recipe.music_volume if mu_volume is None else mu_volume
    return [
        # Split the voice: one branch to the mix, one as the sidechain
        # detector.
        f"{vo_src}volume={recipe.voiceover_volume},asplit=2[vo][sc]",
        f"{mu_src}volume={bed_volume}[bgpre]",
        "[bgpre][sc]sidechaincompress="
        "threshold=0.02:ratio=12:attack=15:release=400:makeup=1[bg]",
        "[vo][bg]amix=inputs=2:duration=longest:dropout_transition=2[mix]",
        f"[mix]loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]",
    ]


def _mix_beds(entries: List[tuple[str, float]]) -> tuple[List[str], str]:
    """Fold N background sources (source audio, music) into one label.

    `entries` is `[(filter_label, volume)]`. `normalize=0` keeps amix
    from halving every input's level when a second bed joins.
    """
    if len(entries) == 1:
        label, volume = entries[0]
        return [f"{label}volume={volume}[bedbus]"], "[bedbus]"
    parts: List[str] = []
    ins: List[str] = []
    for i, (label, volume) in enumerate(entries):
        parts.append(f"{label}volume={volume}[bed{i}]")
        ins.append(f"[bed{i}]")
    parts.append(
        "".join(ins)
        + f"amix=inputs={len(entries)}:duration=longest:dropout_transition=2:normalize=0[bedbus]"
    )
    return parts, "[bedbus]"


def _source_audio_participates(inputs: "ComposeInputs") -> bool:
    """True when the scene videos' own audio should reach the output.

    Only the concat path has source audio; the slideshow path builds
    video from stills. `drop` (the default) keeps every pre-existing
    recipe's argv byte-identical.
    """
    return bool(inputs.scene_video_keys) and inputs.source_audio_mode in ("keep", "duck")


def build_compose_command(
    *,
    inputs: ComposeInputs,
    preset_key: str,
    output_path: str,
    concat_list_path: str,
    recipe: Optional[ComposeRecipe] = None,
    kinetic_ass_path: Optional[str] = None,
) -> List[str]:
    """Build the ffmpeg argv as a pure function of its inputs.

    Dispatches between two pipelines based on which `scene_*_keys` list
    is populated:
      - `scene_video_keys`  → concat-demuxer reel pipeline (existing).
      - `scene_image_keys`  → looped-image slideshow pipeline (new).

    `concat_list_path` is required for the reel pipeline; the slideshow
    pipeline ignores it (each image becomes its own `-loop 1 -t` input).
    """
    if inputs.scene_image_keys and not inputs.scene_video_keys:
        return _build_slideshow_command(
            inputs=inputs,
            preset_key=preset_key,
            output_path=output_path,
            recipe=recipe,
            kinetic_ass_path=kinetic_ass_path,
        )
    # Existing reel/video pipeline below — falls through to the concat
    # demuxer path.
    preset = _preset(preset_key)
    has_scene_vo = bool(inputs.scene_voiceovers)
    recipe = recipe or ComposeRecipe(
        preset_key=preset_key,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        audio_lufs=preset.audio_lufs,
        container=preset.container,
        has_voiceover=(inputs.voiceover_key is not None) or has_scene_vo,
        has_music=inputs.music_key is not None,
    )

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]

    # Input 0: concat demuxer scene list.
    cmd += ["-f", "concat", "-safe", "0", "-i", concat_list_path]

    next_index = 1

    # Voice source: per-scene clips (preferred) OR the legacy single
    # voiceover file. Never both — the render worker picks one.
    scene_vo_indices: List[int] = []
    vo_input_index: Optional[int] = None
    if has_scene_vo:
        for sv in inputs.scene_voiceovers:
            cmd += ["-i", _local_path_for(sv.s3_key)]
            scene_vo_indices.append(next_index)
            next_index += 1
    elif inputs.voiceover_key is not None:
        cmd += ["-i", _local_path_for(inputs.voiceover_key)]
        vo_input_index = next_index
        next_index += 1

    mu_input_index: Optional[int] = None
    if inputs.music_key is not None:
        cmd += ["-i", _local_path_for(inputs.music_key)]
        mu_input_index = next_index
        next_index += 1

    # Silent audio input is declared HERE (before -filter_complex/-map)
    # when neither voiceover nor music is present. ffmpeg's argv is
    # positional — declaring `-f lavfi -i anullsrc=…` after a `-map`
    # makes the parser treat it as an output spec (raises "Option map
    # cannot be applied to input url anullsrc=…").
    keeps_source_audio = _source_audio_participates(inputs)

    silent_audio_index: Optional[int] = None
    if not recipe.has_voiceover and not recipe.has_music and not keeps_source_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        silent_audio_index = next_index
        next_index += 1

    # Build filter_complex for video treatment + audio mixing.
    filter_parts: List[str] = []

    # Video chain: scale/pad safety net, then per-scene on-screen text.
    # Text is windowed by cumulative scene timestamps (`enable=between`)
    # because concat merged all scenes into one stream.
    video_chain = (
        f"[0:v]scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
        f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={preset.fps}"
    )
    for st in inputs.scene_texts:
        # kinetic_typography renders via the ASS track below, not drawtext.
        if st.text.strip() and st.style != "kinetic_typography":
            video_chain += "," + _drawtext_filter(st, windowed=True)
    if kinetic_ass_path:
        video_chain += f",subtitles={kinetic_ass_path}"
    filter_parts.append(video_chain + "[vout]")

    # Voice bus: per-scene clips get delayed to their scene starts
    # (offsets from cumulative scenario durations, computed by the
    # worker into SceneText-compatible ordering via scene_durations_sec).
    vo_src: Optional[str] = None
    if has_scene_vo:
        offsets = _scene_offsets(
            inputs.scene_durations_sec, [sv.scene_pos for sv in inputs.scene_voiceovers], fade=0.0
        )
        filter_parts.extend(_scene_voice_bus(scene_vo_indices, offsets))
        vo_src = "[vobus]"
    elif vo_input_index is not None:
        vo_src = f"[{vo_input_index}:a]"

    audio_label = None
    if keeps_source_audio:
        # Repurpose path — the cut segments carry the source's own
        # (usually trending) audio. `[0:a]` is the concat demuxer's
        # audio stream. Beds are pre-mixed into one label so the
        # ducking filter below stays a two-input graph.
        beds: List[tuple[str, float]] = [("[0:a]", inputs.source_audio_volume if vo_src else 1.0)]
        if mu_input_index is not None:
            beds.append((f"[{mu_input_index}:a]", recipe.music_volume))
        bed_parts, bed_label = _mix_beds(beds)
        filter_parts.extend(bed_parts)
        if vo_src is not None:
            # Voiceover on top: the whole bed (source + music) ducks
            # under speech, same sidechain graph as music-only does.
            filter_parts.extend(
                _ducked_audio_filters(recipe, vo_src=vo_src, mu_src=bed_label, mu_volume=1.0)
            )
        else:
            filter_parts.append(
                f"{bed_label}loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
            )
        audio_label = "[aout]"
    elif vo_src is not None and mu_input_index is not None:
        # Sidechain ducking — music dips ~12dB under speech instead of
        # the old fixed-volume amix fight.
        filter_parts.extend(
            _ducked_audio_filters(recipe, vo_src=vo_src, mu_src=f"[{mu_input_index}:a]")
        )
        audio_label = "[aout]"
    elif vo_src is not None:
        filter_parts.append(
            f"{vo_src}volume={recipe.voiceover_volume},loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
        )
        audio_label = "[aout]"
    elif mu_input_index is not None:
        filter_parts.append(
            f"[{mu_input_index}:a]volume={recipe.music_volume},loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
        )
        audio_label = "[aout]"
    # else: `source_audio_mode='drop'` and no voiceover/music — the scene
    # videos' own audio is discarded and the silent input declared above
    # is mapped instead.

    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]

    cmd += ["-map", "[vout]"]
    if audio_label is not None:
        cmd += ["-map", audio_label]
    elif silent_audio_index is not None:
        # Map the silent anullsrc input declared above (before
        # -filter_complex). `-shortest` further down trims the output
        # to video length. Do NOT add `-t 1` to the input — it caps
        # the silent stream to 1 second and `-shortest` would then
        # truncate the FINAL video to 1s.
        cmd += ["-map", f"{silent_audio_index}:a"]

    # Encode settings.
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-shortest",
        "-movflags",
        "+faststart",
        output_path,
    ]
    return cmd


def _build_slideshow_command(
    *,
    inputs: ComposeInputs,
    preset_key: str,
    output_path: str,
    recipe: Optional[ComposeRecipe] = None,
    kinetic_ass_path: Optional[str] = None,
) -> List[str]:
    """Build the ffmpeg argv for the image-slideshow pipeline.

    One `-loop 1 -t <dur> -i <image>` input per scene, scaled and padded
    to the preset's canvas dimensions, then concatenated. Voiceover +
    music are mixed identically to the video pipeline; the only
    difference is video input handling.

    A subtle Ken-Burns push-in is applied per image so static images
    don't look stale on a slideshow feed. Each input gets `zoompan` at
    the preset's fps; total frames per scene = duration * fps.

    Requires:
      - `inputs.scene_image_keys` non-empty
      - `inputs.scene_durations_sec` matches in length

    `concat_list_path` is intentionally not used here — each image is
    its own input, not a demuxer entry.
    """
    if len(inputs.scene_image_keys) != len(inputs.scene_durations_sec):
        raise FFmpegError(
            "scene_image_keys and scene_durations_sec must have equal length"
        )
    preset = _preset(preset_key)
    has_scene_vo = bool(inputs.scene_voiceovers)
    recipe = recipe or ComposeRecipe(
        preset_key=preset_key,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        audio_lufs=preset.audio_lufs,
        container=preset.container,
        has_voiceover=(inputs.voiceover_key is not None) or has_scene_vo,
        has_music=inputs.music_key is not None,
    )

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]

    # One single-frame image input per scene. Do NOT use `-loop 1 -t`
    # here: zoompan below already synthesizes `duration × fps` output
    # frames from ONE input frame via its `d=` parameter. Looping the
    # input multiplies the two (each looped frame gets its own zoompan
    # run) and a 2s scene balloons to minutes of output — the bug that
    # made early slideshow composes crawl past the job timeout.
    for image_key in inputs.scene_image_keys:
        cmd += ["-i", _local_path_for(image_key)]
    next_index = len(inputs.scene_image_keys)

    # Voice source: per-scene clips (preferred) or the legacy single file.
    scene_vo_indices: List[int] = []
    vo_input_index: Optional[int] = None
    if has_scene_vo:
        for sv in inputs.scene_voiceovers:
            cmd += ["-i", _local_path_for(sv.s3_key)]
            scene_vo_indices.append(next_index)
            next_index += 1
    elif inputs.voiceover_key is not None:
        cmd += ["-i", _local_path_for(inputs.voiceover_key)]
        vo_input_index = next_index
        next_index += 1

    mu_input_index: Optional[int] = None
    if inputs.music_key is not None:
        cmd += ["-i", _local_path_for(inputs.music_key)]
        mu_input_index = next_index
        next_index += 1

    # If neither voiceover nor music is present, attach a silent audio
    # input HERE (before filter_complex / -map). ffmpeg parses argv
    # positionally; declaring `-f lavfi -i anullsrc=…` after a `-map`
    # is interpreted as an OUTPUT spec ("Option map cannot be applied
    # to input url anullsrc=…"). The silent track keeps the final mp4's
    # audio track present so platforms that require AAC don't reject
    # the upload.
    silent_audio_index: Optional[int] = None
    if not recipe.has_voiceover and not recipe.has_music:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        silent_audio_index = next_index
        next_index += 1

    filter_parts: List[str] = []
    n_scenes = len(inputs.scene_image_keys)

    # Index scene texts by their render position so each per-scene
    # chain can carry its own drawtext (chain clock starts at 0 —
    # no enable-window needed). kinetic_typography texts are excluded:
    # they render through the ASS track applied post-join.
    texts_by_pos: dict[int, SceneText] = {
        st.scene_pos: st
        for st in inputs.scene_texts
        if st.text.strip() and st.style != "kinetic_typography"
    }

    # Per-scene: scale → pad → setsar → Ken-Burns zoompan → fps lock →
    # optional drawtext for the scene's on-screen text.
    # zoompan needs total frames = duration * fps. We push from 1.0 to
    # ~1.08 (8% zoom) to keep the image alive without obvious crops.
    for i, duration in enumerate(inputs.scene_durations_sec):
        frames = max(1, int(round(float(duration) * preset.fps)))
        chain = (
            f"[{i}:v]scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
            f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
            # zoompan generates `d` output frames from the single input
            # frame. Pin its fps to the preset so d=duration×fps yields
            # exactly `duration` seconds (default 25fps would stretch a
            # 30fps frame count to duration×30/25).
            f"zoompan=z='min(zoom+0.0008,1.08)':d={frames}:s={preset.width}x{preset.height}:fps={preset.fps},"
            f"fps={preset.fps},format=yuv420p"
        )
        st = texts_by_pos.get(i)
        if st is not None:
            chain += "," + _drawtext_filter(st, windowed=False)
        # xfade needs settable PTS — give each chain a clean timestamp base.
        chain += f",settb=AVTB,setpts=PTS-STARTPTS[v{i}]"
        filter_parts.append(chain)

    # Join scenes. When any boundary requests a fade we build an xfade
    # chain (offset math below); all-cut scenarios keep the cheaper
    # plain concat. `scene_transitions[i]` is scene i's transition OUT,
    # so boundary i (between scene i and i+1) reads transitions[i].
    _FADE_DUR = 0.35
    transitions = list(inputs.scene_transitions or [])
    wants_fade = _slideshow_wants_fade(transitions, n_scenes)
    # Kinetic ASS burns onto the JOINED stream, so [vout] must be built
    # first and the subtitles filter appended to a final pass-through.
    join_out = "[vjoin]" if kinetic_ass_path else "[vout]"
    if n_scenes == 1:
        filter_parts.append(f"[v0]null{join_out}")
    elif not wants_fade:
        concat_inputs = "".join(f"[v{i}]" for i in range(n_scenes))
        filter_parts.append(f"{concat_inputs}concat=n={n_scenes}:v=1:a=0{join_out}")
    else:
        # xfade chain: [v0][v1]xfade[x1]; [x1][v2]xfade[x2]; …
        # offset_i = (sum of durations up to scene i) − (i × fade).
        # UNIFORM fade on every boundary — mixing per-boundary micro
        # fades for "cut" hits an xfade edge case where the chain
        # truncates at the boundary (verified empirically); a scenario
        # that wants fades gets them everywhere, which also reads more
        # cohesively than alternating cut/fade.
        prev_label = "[v0]"
        elapsed = 0.0
        for i in range(1, n_scenes):
            prev_duration = float(inputs.scene_durations_sec[i - 1])
            elapsed += prev_duration - _FADE_DUR
            out_label = join_out if i == n_scenes - 1 else f"[x{i}]"
            filter_parts.append(
                f"{prev_label}[v{i}]xfade=transition=fade:duration={_FADE_DUR:.2f}:offset={max(0.0, elapsed):.3f}{out_label}"
            )
            prev_label = out_label

    # Burn kinetic word-pop subtitles onto the joined stream.
    if kinetic_ass_path:
        filter_parts.append(f"[vjoin]subtitles={kinetic_ass_path}[vout]")

    # Voice bus. Per-scene clips are delayed to their scene starts;
    # when xfade is active every boundary pulls later scenes ~0.35s
    # earlier, so offsets compensate with the same fade width.
    vo_src: Optional[str] = None
    if has_scene_vo:
        offset_fade = _FADE_DUR if (n_scenes > 1 and wants_fade) else 0.0
        offsets = _scene_offsets(
            inputs.scene_durations_sec,
            [sv.scene_pos for sv in inputs.scene_voiceovers],
            fade=offset_fade,
        )
        filter_parts.extend(_scene_voice_bus(scene_vo_indices, offsets))
        vo_src = "[vobus]"
    elif vo_input_index is not None:
        vo_src = f"[{vo_input_index}:a]"

    audio_label = None
    if vo_src is not None and mu_input_index is not None:
        # Sidechain ducking — same treatment as the video pipeline.
        filter_parts.extend(
            _ducked_audio_filters(recipe, vo_src=vo_src, mu_src=f"[{mu_input_index}:a]")
        )
        audio_label = "[aout]"
    elif vo_src is not None:
        filter_parts.append(
            f"{vo_src}volume={recipe.voiceover_volume},"
            f"loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
        )
        audio_label = "[aout]"
    elif mu_input_index is not None:
        filter_parts.append(
            f"[{mu_input_index}:a]volume={recipe.music_volume},"
            f"loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
        )
        audio_label = "[aout]"

    cmd += ["-filter_complex", ";".join(filter_parts)]
    cmd += ["-map", "[vout]"]

    if audio_label is not None:
        cmd += ["-map", audio_label]
    elif silent_audio_index is not None:
        # Map the silent anullsrc input that we added BEFORE
        # -filter_complex above.
        cmd += ["-map", f"{silent_audio_index}:a"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def _local_path_for(s3_key: str) -> str:
    """Map an S3 key to its local copy path inside the workdir.

    The `compose_variant` orchestrator downloads each unique S3 key once
    into `<workdir>/in/<basename>` before running ffmpeg. We reproduce the
    same mapping here so `build_compose_command` is purely testable.
    """
    return f"in/{Path(s3_key).name}"


def write_concat_list(workdir: Path, scene_video_keys: List[str]) -> Path:
    """Write the ffmpeg concat demuxer file list to disk."""
    lines = []
    for key in scene_video_keys:
        local = (workdir / _local_path_for(key)).resolve()
        # ffmpeg concat demuxer: single-quoted, escape internal apostrophes.
        escaped = str(local).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path = workdir / "concat.txt"
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_path


def compose_variant(
    *,
    project_id,
    scenario_id,
    preset_key: str,
    inputs: ComposeInputs,
    output_filename: str,
    workdir: Optional[Path] = None,
) -> dict:
    """Orchestrate the full compose flow.

    Returns `{s3_key, file_size_bytes, recipe, ffmpeg_cmd}`.
    Raises `FFmpegError` on failure.
    """
    cleanup_dir: Optional[tempfile.TemporaryDirectory] = None
    if workdir is None:
        cleanup_dir = tempfile.TemporaryDirectory(prefix="compose-")
        workdir = Path(cleanup_dir.name)
    in_dir = workdir / "in"
    in_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Download inputs.
        for key in _all_input_keys(inputs):
            local = workdir / _local_path_for(key)
            local.parent.mkdir(parents=True, exist_ok=True)
            _download_to(key, local)

        # The slideshow pipeline doesn't use a concat-demuxer file list
        # (each image is its own `-loop 1` input), but `build_compose_command`
        # still accepts the arg — pass a no-op path so signatures stay
        # uniform.
        if inputs.scene_image_keys and not inputs.scene_video_keys:
            concat_list = workdir / "concat.unused.txt"
            concat_list.write_text("", encoding="utf-8")
        else:
            concat_list = write_concat_list(workdir, inputs.scene_video_keys)
        output_path = workdir / output_filename
        recipe = ComposeRecipe(
            preset_key=preset_key,
            width=PRESETS[preset_key].width,
            height=PRESETS[preset_key].height,
            fps=PRESETS[preset_key].fps,
            audio_lufs=PRESETS[preset_key].audio_lufs,
            container=PRESETS[preset_key].container,
            has_voiceover=(inputs.voiceover_key is not None) or bool(inputs.scene_voiceovers),
            has_music=inputs.music_key is not None,
            extra={
                "scene_voiceovers": len(inputs.scene_voiceovers),
                "outro": bool(inputs.outro_video_key),
                "kinetic": any(
                    st.style == "kinetic_typography" for st in inputs.scene_texts
                ),
            },
        )
        # Kinetic typography — generate the ASS track when any scene
        # opted into it. A bare relative filename keeps the subtitles
        # filter free of path-escaping pain (ffmpeg runs with
        # cwd=workdir).
        kinetic_ass_path: Optional[str] = None
        kinetic_texts = [
            st
            for st in inputs.scene_texts
            if st.style == "kinetic_typography" and st.text.strip()
        ]
        if kinetic_texts:
            is_slideshow = bool(inputs.scene_image_keys and not inputs.scene_video_keys)
            fade = (
                0.35
                if is_slideshow
                and _slideshow_wants_fade(
                    list(inputs.scene_transitions or []), len(inputs.scene_image_keys)
                )
                else 0.0
            )
            positions = [st.scene_pos for st in kinetic_texts]
            offsets = _scene_offsets(inputs.scene_durations_sec, positions, fade=fade)
            durations = [
                float(inputs.scene_durations_sec[p])
                if 0 <= p < len(inputs.scene_durations_sec)
                else max(0.5, st.end_sec - st.start_sec)
                for p, st in zip(positions, kinetic_texts)
            ]
            content = build_kinetic_ass(
                kinetic_texts,
                offsets_sec=offsets,
                durations_sec=durations,
                width=PRESETS[preset_key].width,
                height=PRESETS[preset_key].height,
            )
            if content:
                (workdir / "kinetic.ass").write_text(content, encoding="utf-8")
                kinetic_ass_path = "kinetic.ass"

        cmd = build_compose_command(
            inputs=inputs,
            preset_key=preset_key,
            output_path=str(output_path),
            concat_list_path=str(concat_list),
            recipe=recipe,
            kinetic_ass_path=kinetic_ass_path,
        )

        logger.info("ffmpeg_compose_starting", cmd=" ".join(shlex.quote(arg) for arg in cmd))
        try:
            result = subprocess.run(  # noqa: S603 (ffmpeg argv is fully constructed; no shell)
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FFmpegError("ffmpeg binary not found in PATH") from exc

        if result.returncode != 0:
            raise FFmpegError(
                f"ffmpeg exited {result.returncode}: {result.stderr[-2000:].strip() or result.stdout[-2000:].strip()}"
            )

        # Outro append — second normalization pass. Runs AFTER the main
        # compose so the outro can be any codec/dims/fps; the concat
        # filter re-encodes both parts to the preset's canvas. Fail-open:
        # a broken outro logs a warning and ships the main video alone
        # rather than failing the whole variant.
        if inputs.outro_video_key:
            try:
                appended = _append_outro(
                    workdir=workdir,
                    main_path=output_path,
                    outro_local=workdir / _local_path_for(inputs.outro_video_key),
                    preset_key=preset_key,
                )
                output_path = appended
            except FFmpegError as exc:
                logger.warning("outro_append_failed", error=str(exc))

        size_bytes = output_path.stat().st_size

        # Upload final.
        out_key = s3.make_key(project_id, "finals", output_filename)
        with output_path.open("rb") as fh:
            s3.upload_bytes(out_key, fh.read(), content_type="video/mp4")

        return {
            "s3_key": out_key,
            "file_size_bytes": size_bytes,
            "recipe": _recipe_to_dict(recipe),
            "ffmpeg_cmd": cmd,
        }
    finally:
        if cleanup_dir is not None:
            cleanup_dir.cleanup()


def _all_input_keys(inputs: ComposeInputs) -> List[str]:
    keys: List[str] = list(inputs.scene_video_keys) + list(inputs.scene_image_keys)
    keys.extend(sv.s3_key for sv in inputs.scene_voiceovers)
    if inputs.voiceover_key:
        keys.append(inputs.voiceover_key)
    if inputs.music_key:
        keys.append(inputs.music_key)
    if inputs.outro_video_key:
        keys.append(inputs.outro_video_key)
    return keys


def _download_to(key: str, dest: Path) -> None:
    """Download an S3 object to a local path."""
    body = s3.client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"]
    with dest.open("wb") as fh:
        fh.write(body.read())


def _append_outro(
    *, workdir: Path, main_path: Path, outro_local: Path, preset_key: str
) -> Path:
    """Append the outro to the composed video via a normalization pass.

    The outro can be any codec / dims / fps — both parts run through
    scale+pad+fps and a filter-graph concat, then re-encode. Audio is
    normalized to stereo 48kHz on both sides (an outro without audio
    gets silence injected via `apad`-equivalent anullsrc mapping).

    Returns the path of the appended output (a new file next to the
    main one). Raises FFmpegError on any ffmpeg failure.
    """
    preset = _preset(preset_key)
    out_path = main_path.with_name(main_path.stem + "-outro" + main_path.suffix)
    norm = (
        f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
        f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={preset.fps},"
        f"format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS"
    )
    fc = (
        f"[0:v]{norm}[v0];"
        f"[1:v]{norm}[v1];"
        # `aresample=async=1` guards against outros whose audio stream
        # starts late; missing audio on either side is the caller's
        # responsibility (template validation should require a track,
        # but concat with `a=1` fails loudly rather than silently
        # producing a broken file — acceptable).
        "[0:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a0];"
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-loglevel", "warning",
        "-i", str(main_path),
        "-i", str(outro_local),
        "-filter_complex", fc,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, cwd=str(workdir), capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise FFmpegError("ffmpeg binary not found in PATH") from exc
    if result.returncode != 0:
        raise FFmpegError(
            f"outro append exited {result.returncode}: {result.stderr[-1500:].strip()}"
        )
    return out_path


def _recipe_to_dict(recipe: ComposeRecipe) -> dict:
    return {
        "preset_key": recipe.preset_key,
        "width": recipe.width,
        "height": recipe.height,
        "fps": recipe.fps,
        "audio_lufs": recipe.audio_lufs,
        "container": recipe.container,
        "has_voiceover": recipe.has_voiceover,
        "has_music": recipe.has_music,
        "music_volume": recipe.music_volume,
        "voiceover_volume": recipe.voiceover_volume,
        "extra": recipe.extra,
    }
