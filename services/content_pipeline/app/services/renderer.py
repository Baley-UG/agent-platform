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
    voiceover_key: Optional[str] = None
    music_key: Optional[str] = None
    outro_video_key: Optional[str] = None  # CP-M5.5+ — currently ignored by build_compose_command


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


def build_compose_command(
    *,
    inputs: ComposeInputs,
    preset_key: str,
    output_path: str,
    concat_list_path: str,
    recipe: Optional[ComposeRecipe] = None,
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
        )
    # Existing reel/video pipeline below — falls through to the concat
    # demuxer path.
    preset = _preset(preset_key)
    recipe = recipe or ComposeRecipe(
        preset_key=preset_key,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        audio_lufs=preset.audio_lufs,
        container=preset.container,
        has_voiceover=inputs.voiceover_key is not None,
        has_music=inputs.music_key is not None,
    )

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]

    # Input 0: concat demuxer scene list.
    cmd += ["-f", "concat", "-safe", "0", "-i", concat_list_path]

    audio_input_indices: List[int] = []
    next_index = 1

    if inputs.voiceover_key is not None:
        # Voiceover input gets index 1.
        cmd += ["-i", _local_path_for(inputs.voiceover_key)]
        audio_input_indices.append(next_index)
        next_index += 1

    if inputs.music_key is not None:
        cmd += ["-i", _local_path_for(inputs.music_key)]
        audio_input_indices.append(next_index)
        next_index += 1

    # Silent audio input is declared HERE (before -filter_complex/-map)
    # when neither voiceover nor music is present. ffmpeg's argv is
    # positional — declaring `-f lavfi -i anullsrc=…` after a `-map`
    # makes the parser treat it as an output spec (raises "Option map
    # cannot be applied to input url anullsrc=…").
    silent_audio_index: Optional[int] = None
    if not inputs.voiceover_key and not inputs.music_key:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        silent_audio_index = next_index
        next_index += 1

    # Build filter_complex for audio mixing + loudness.
    filter_parts: List[str] = []

    # Always re-scale + pad as a safety net so we end up at exactly the preset dims.
    filter_parts.append(
        f"[0:v]scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
        f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={preset.fps}[vout]"
    )

    audio_label = None
    if recipe.has_voiceover and recipe.has_music:
        # Voiceover at full volume, music at recipe.music_volume, then amix.
        filter_parts.append(f"[1:a]volume={recipe.voiceover_volume}[vo]")
        filter_parts.append(f"[2:a]volume={recipe.music_volume}[bg]")
        filter_parts.append(
            "[vo][bg]amix=inputs=2:duration=longest:dropout_transition=2[mix]"
        )
        filter_parts.append(f"[mix]loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]")
        audio_label = "[aout]"
    elif recipe.has_voiceover:
        filter_parts.append(f"[1:a]volume={recipe.voiceover_volume},loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]")
        audio_label = "[aout]"
    elif recipe.has_music:
        filter_parts.append(f"[1:a]volume={recipe.music_volume},loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]")
        audio_label = "[aout]"
    # else: no audio inputs; the concat scene videos may carry their own audio
    # which we silently drop in CP-M5 (an admin who wanted the original audio
    # would set music_volume=0 and skip TTS — a corner case for CP-M5.5).

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
    recipe = recipe or ComposeRecipe(
        preset_key=preset_key,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        audio_lufs=preset.audio_lufs,
        container=preset.container,
        has_voiceover=inputs.voiceover_key is not None,
        has_music=inputs.music_key is not None,
    )

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning"]

    # Add one looped-image input per scene.
    for image_key, duration in zip(inputs.scene_image_keys, inputs.scene_durations_sec):
        cmd += [
            "-loop", "1",
            "-t", f"{max(0.5, float(duration)):.3f}",
            "-i", _local_path_for(image_key),
        ]
    next_index = len(inputs.scene_image_keys)

    audio_input_indices: List[int] = []
    if inputs.voiceover_key is not None:
        cmd += ["-i", _local_path_for(inputs.voiceover_key)]
        audio_input_indices.append(next_index)
        next_index += 1
    if inputs.music_key is not None:
        cmd += ["-i", _local_path_for(inputs.music_key)]
        audio_input_indices.append(next_index)
        next_index += 1

    # If neither voiceover nor music is present, attach a silent audio
    # input HERE (before filter_complex / -map). ffmpeg parses argv
    # positionally; declaring `-f lavfi -i anullsrc=…` after a `-map`
    # is interpreted as an OUTPUT spec ("Option map cannot be applied
    # to input url anullsrc=…"). The silent track keeps the final mp4's
    # audio track present so platforms that require AAC don't reject
    # the upload.
    silent_audio_index: Optional[int] = None
    if not inputs.voiceover_key and not inputs.music_key:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        silent_audio_index = next_index
        next_index += 1

    filter_parts: List[str] = []
    n_scenes = len(inputs.scene_image_keys)

    # Per-scene: scale → pad → setsar → Ken-Burns zoompan → fps lock.
    # zoompan needs total frames = duration * fps. We push from 1.0 to
    # ~1.08 (8% zoom) to keep the image alive without obvious crops.
    for i, duration in enumerate(inputs.scene_durations_sec):
        frames = max(1, int(round(float(duration) * preset.fps)))
        filter_parts.append(
            f"[{i}:v]scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,"
            f"pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
            f"zoompan=z='min(zoom+0.0008,1.08)':d={frames}:s={preset.width}x{preset.height},"
            f"fps={preset.fps},format=yuv420p"
            f"[v{i}]"
        )
    # Concatenate all per-scene streams into one [vout].
    concat_inputs = "".join(f"[v{i}]" for i in range(n_scenes))
    filter_parts.append(f"{concat_inputs}concat=n={n_scenes}:v=1:a=0[vout]")

    # Audio mix — identical to the video pipeline below this function.
    audio_label = None
    vo_idx = audio_input_indices[0] if recipe.has_voiceover else None
    mu_idx = audio_input_indices[1 if recipe.has_voiceover else 0] if recipe.has_music else None

    if recipe.has_voiceover and recipe.has_music:
        filter_parts.append(f"[{vo_idx}:a]volume={recipe.voiceover_volume}[vo]")
        filter_parts.append(f"[{mu_idx}:a]volume={recipe.music_volume}[bg]")
        filter_parts.append(
            "[vo][bg]amix=inputs=2:duration=longest:dropout_transition=2[mix]"
        )
        filter_parts.append(f"[mix]loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]")
        audio_label = "[aout]"
    elif recipe.has_voiceover:
        filter_parts.append(
            f"[{vo_idx}:a]volume={recipe.voiceover_volume},"
            f"loudnorm=I={recipe.audio_lufs}:TP=-1.5:LRA=11[aout]"
        )
        audio_label = "[aout]"
    elif recipe.has_music:
        filter_parts.append(
            f"[{mu_idx}:a]volume={recipe.music_volume},"
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
            has_voiceover=inputs.voiceover_key is not None,
            has_music=inputs.music_key is not None,
        )
        cmd = build_compose_command(
            inputs=inputs,
            preset_key=preset_key,
            output_path=str(output_path),
            concat_list_path=str(concat_list),
            recipe=recipe,
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
