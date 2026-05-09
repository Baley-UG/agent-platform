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
    scene_video_keys: List[str]
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

    `concat_list_path` is the path to a concat-demuxer file list that the
    caller has already written to disk. Each line:
        file '/path/to/scene_0.mp4'
    """
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
    else:
        # Encode silent audio so platforms that require an audio track don't reject the upload.
        cmd += [
            "-f",
            "lavfi",
            "-t",
            "1",  # placeholder; -shortest below trims to video.
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
        cmd += ["-map", f"{next_index}:a"]

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
    keys: List[str] = list(inputs.scene_video_keys)
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
