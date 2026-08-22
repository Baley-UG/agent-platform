"""ffmpeg segment cutting for `repurpose` production mode.

Takes one source video plus a cut list and produces N normalized mp4
clips — one per `keep` segment — ready for the compose stage's concat
demuxer.

Split the same way as `renderer.py`: `build_multicut_command` is a pure
argv builder (unit-testable without ffmpeg on PATH), `cut_segments`
does the S3 download / run / upload orchestration.

Two decisions worth not re-litigating:

- **One process, one decode, N outputs.** ffmpeg accepts repeated
  output specs against a single `-i`. Cutting per-segment in separate
  jobs would re-download and re-decode the same mp4 once per segment.
- **Output-side `-ss`/`-t` with re-encode.** Input-side fast seek lands
  on the nearest keyframe, which drifts the first frames of every clip.
  Frame accuracy matters here because the segment boundaries ARE the
  scene boundaries the whole mode is built on. If long sources ever
  make this too slow, `_seek_args` is the single place to switch to the
  hybrid (input `-ss start-0.5` + output `-ss 0.5`) form.

Runs in the render container — the generic worker image has no ffmpeg.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.services.presets import ASPECT_DIMENSIONS
from app.services.video_frames import _probe_duration


@dataclass
class Cut:
    """One time window to cut out of a source video.

    Deliberately minimal — just the geometry the ffmpeg cutter needs.
    The remake pipeline's creative fields (technique, prompt) live on
    `remake_shots`; this dataclass stays a pure cut instruction so the
    argv builder is trivially unit-testable.
    """

    idx: int
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return round(self.end_sec - self.start_sec, 3)


DEFAULT_FPS = 30
DEFAULT_CRF = 20
DEFAULT_PRESET = "veryfast"
CUT_TIMEOUT_SECONDS = 1800


class SegmentCutError(RuntimeError):
    """ffmpeg failed, or the source is unusable."""


def normalize_filter(aspect: str, fps: int = DEFAULT_FPS, fit_mode: str = "cover") -> str:
    """Video filter that makes every cut byte-compatible for concat.

    `cover` (default) scales up and centre-crops so the frame fills the
    canvas — a letterboxed reel reads as broken on IG, and cropping also
    removes many corner watermarks for free. `contain` letterboxes
    instead, matching the pad shape the compose stage already uses.

    `settb`/`setpts` reset the timebase and PTS so concat doesn't
    inherit the source's timestamps.
    """
    width, height = ASPECT_DIMENSIONS.get(aspect, ASPECT_DIMENSIONS["9:16"])
    if fit_mode == "contain":
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    else:
        fit = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    return f"{fit},setsar=1,fps={fps},format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS"


def normalize_audio_filter() -> str:
    """Uniform sample rate + layout so concat doesn't stall on a
    mid-stream format change. The source audio is KEPT — repurpose
    defaults to republishing the original (trend) sound, and compose
    decides later whether to duck or drop it."""
    return "aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS"


def _seek_args(start_sec: float, duration_sec: float) -> List[str]:
    """Output-side seek — frame-accurate at the cost of decoding from 0."""
    return ["-ss", f"{start_sec:.3f}", "-t", f"{duration_sec:.3f}"]


def build_multicut_command(
    *,
    src_path: str,
    segments: List[Cut],
    aspect: str,
    out_paths: List[str],
    fps: int = DEFAULT_FPS,
    fit_mode: str = "cover",
    crf: int = DEFAULT_CRF,
    src_has_audio: bool = True,
) -> List[str]:
    """One ffmpeg argv that writes every segment in `segments`.

    `out_paths` must be index-aligned with `segments`.

    EVERY produced clip carries an aac stereo 48k audio track — even when
    the source has none. The concat demuxer that compose uses requires a
    uniform stream layout across all clips, so a single silent source
    shot would otherwise break (or silently truncate) the whole compose.
    When the source has no audio we synthesize silence from `anullsrc`.
    """
    if len(out_paths) != len(segments):
        raise SegmentCutError(
            f"out_paths/segments length mismatch: {len(out_paths)} vs {len(segments)}"
        )
    if not segments:
        raise SegmentCutError("no segments to cut")

    vf = normalize_filter(aspect, fps=fps, fit_mode=fit_mode)
    af = normalize_audio_filter()

    cmd = ["ffmpeg", "-hide_banner", "-y", "-loglevel", "warning", "-i", src_path]
    if not src_has_audio:
        # Input 1: infinite silence, trimmed per-output by -t / -shortest.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    for seg, out_path in zip(segments, out_paths):
        cmd += _seek_args(seg.start_sec, seg.duration_sec)
        if src_has_audio:
            cmd += ["-map", "0:v:0", "-map", "0:a:0", "-af", af]
        else:
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", DEFAULT_PRESET,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",
            "-ac", "2",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path,
        ]
    return cmd


def _probe_has_audio(video_path: str) -> bool:
    """True when the file carries at least one audio stream."""
    if not shutil.which("ffprobe"):
        return True  # assume audio; the source-audio path is the common case
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
            stderr=subprocess.PIPE, timeout=30,
        )
        return bool(out.strip())
    except (subprocess.CalledProcessError, OSError):
        return True


def s3_key_for_segment(
    project_id, scenario_id, idx: int, aspect: str
) -> str:
    """Key for one cut segment, in the same namespace as scene videos.

    Routed through `s3.make_key` for two reasons: it honours
    `S3_ROOT_PREFIX` (so the bucket can be shared with other services),
    and its uuid component makes every cut a fresh object. The latter
    matters — a re-cut writes a NEW `media_assets` version, and the
    prior version's `s3_key` has to keep pointing at the bytes it was
    created from or rollback silently serves the replacement.
    """
    aspect_slug = aspect.replace(":", "x")
    return s3lib.make_key(
        project_id, "scenes", f"{scenario_id}-segment-{idx:02d}-{aspect_slug}.mp4"
    )


def _download_source(src_s3_key: str, dest_dir: str) -> str:
    _, ext = os.path.splitext(src_s3_key)
    local = os.path.join(dest_dir, f"source{ext or '.mp4'}")
    with open(local, "wb") as fh:
        s3lib.client().download_fileobj(settings.S3_BUCKET, src_s3_key, fh)
    return local


def cut_segments(
    *,
    project_id,
    scenario_id,
    src_s3_key: str,
    segments: List[Cut],
    aspect: str,
    fit_mode: str = "cover",
    fps: int = DEFAULT_FPS,
) -> List[Dict[str, Any]]:
    """Download → cut → upload. Returns one dict per produced clip:
    `{idx, s3_key, size_bytes, duration_sec, width, height, ffmpeg_cmd}`.

    Raises `SegmentCutError` when ffmpeg is missing or exits non-zero —
    the worker turns that into a per-render failure.
    """
    if not shutil.which("ffmpeg"):
        raise SegmentCutError("ffmpeg binary not found on PATH")
    if not segments:
        return []

    width, height = ASPECT_DIMENSIONS.get(aspect, ASPECT_DIMENSIONS["9:16"])
    workdir = tempfile.mkdtemp(prefix="segcut-")
    try:
        src_path = _download_source(src_s3_key, workdir)
        src_has_audio = _probe_has_audio(src_path)

        out_paths = [
            os.path.join(workdir, f"seg-{seg.idx:02d}.mp4") for seg in segments
        ]
        cmd = build_multicut_command(
            src_path=src_path,
            segments=segments,
            aspect=aspect,
            out_paths=out_paths,
            fps=fps,
            fit_mode=fit_mode,
            src_has_audio=src_has_audio,
        )
        logger.info(
            "segment_cut_start",
            scenario_id=str(scenario_id),
            aspect=aspect,
            segments=len(segments),
        )
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                timeout=CUT_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise SegmentCutError(f"ffmpeg segment cut timed out: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").splitlines()[-12:]
            raise SegmentCutError(
                f"ffmpeg exited {proc.returncode}: " + " | ".join(tail)
            )

        results: list[dict] = []
        for seg, path in zip(segments, out_paths):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                raise SegmentCutError(f"segment {seg.idx} produced no output")
            key = s3_key_for_segment(project_id, scenario_id, seg.idx, aspect)
            with open(path, "rb") as fh:
                data = fh.read()
            s3lib.upload_bytes(key, data, content_type="video/mp4")
            # Probe rather than trust the request: re-encoding to a fixed
            # fps rounds the cut out to a whole frame, so a 4.000s window
            # lands at ~4.067s. Downstream timing (text windows, voice
            # offsets) must use what the file actually contains, or the
            # error compounds across a dozen segments.
            actual = _probe_duration(path) or seg.duration_sec
            results.append(
                {
                    "idx": seg.idx,
                    "s3_key": key,
                    "size_bytes": len(data),
                    "duration_sec": actual,
                    "planned_duration_sec": seg.duration_sec,
                    "start_sec": seg.start_sec,
                    "end_sec": seg.end_sec,
                    "width": width,
                    "height": height,
                    "ffmpeg_cmd": " ".join(cmd),
                }
            )
        logger.info(
            "segment_cut_done",
            scenario_id=str(scenario_id),
            aspect=aspect,
            produced=len(results),
        )
        return results
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
