"""ffmpeg-backed keyframe extraction.

When a brand admin uploads a video to the asset library, the director
needs to SEE it — vision LLMs don't watch video, only stills. We pull
6-8 representative frames per video:

  1. Try ffmpeg scene-detection (`select='gt(scene,0.3)'`). When the
     video has real shot boundaries (cuts, transitions, energetic
     movement), this yields semantically meaningful frames.
  2. If scene-detect returns fewer than `min_frames`, supplement with
     evenly-spaced frames to fill the budget.
  3. If scene-detect returns more than `max_frames`, downsample to the
     most-evenly-spaced subset.

Each kept frame becomes a separate `media_assets` row tagged with the
parent video's `source_asset_id` and the timestamp. The director
treats extracted frames as brand-library candidates; compose can later
cut the surrounding segment (Phase 3.5).

Runs inside the render container — the generic worker image doesn't
ship ffmpeg.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.core.logging import logger


class FrameExtractError(RuntimeError):
    """ffmpeg failure or unrecoverable parse failure."""


@dataclass
class ExtractedFrame:
    timestamp_sec: float
    jpeg_bytes: bytes


# Default knobs — tuned for "short brand reel" inputs (5-60s).
DEFAULT_MIN_FRAMES = 4
DEFAULT_MAX_FRAMES = 8
DEFAULT_SCENE_THRESHOLD = 0.30
# Each frame target dimensions. The director only needs the asset at
# thumbnail resolution to pick from; we save bandwidth + LLM token
# cost by downscaling. ffmpeg's `-vf scale='min(1280,iw)':-1` keeps
# aspect ratio.
DEFAULT_MAX_DIMENSION = 1280


def _probe_duration(video_path: str) -> float:
    """Pull the video's duration in seconds via ffprobe.

    Falls back to 0.0 when ffprobe isn't available or returns garbage —
    callers handle 0 gracefully by skipping even-interval supplementation.
    """
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                video_path,
            ],
            stderr=subprocess.PIPE,
            timeout=30,
        )
        data = json.loads(out)
        return float(data.get("format", {}).get("duration") or 0.0)
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        logger.warning("ffprobe_duration_failed", error=str(exc), path=video_path)
        return 0.0


def _scene_detect_frames(
    video_path: str,
    *,
    outdir: str,
    threshold: float,
    max_dim: int,
) -> List[Tuple[float, str]]:
    """Run ffmpeg's `select='gt(scene,T)'` filter and capture the
    frames it picks.

    We use `-vf "select='gt(scene,T)',scale='min(W,iw)':-1,showinfo"`.
    `showinfo` prints each emitted frame's `pts_time` to stderr; we
    parse those to recover timestamps.

    Returns `[(timestamp_sec, frame_path), ...]` in order.
    """
    template = os.path.join(outdir, "scene_%04d.jpg")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        video_path,
        "-vf",
        (
            f"select='gt(scene,{threshold})',"
            f"scale='min({max_dim},iw)':-1,"
            f"showinfo"
        ),
        "-vsync",
        "vfr",
        "-q:v",
        "3",
        template,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise FrameExtractError(f"ffmpeg scene-detect timed out: {exc}") from exc
    if proc.returncode != 0:
        # Surface stderr tail so admin can diagnose codec issues etc.
        tail = (proc.stderr or "").splitlines()[-12:]
        raise FrameExtractError(
            f"ffmpeg scene-detect exited {proc.returncode}: " + " | ".join(tail)
        )

    # Parse showinfo lines: lines look like
    #   [Parsed_showinfo_2 @ 0x7f..] n: 0 pts: 1234 pts_time:1.234 ...
    timestamps: list[float] = []
    for line in (proc.stderr or "").splitlines():
        if "pts_time:" not in line:
            continue
        try:
            tok = line.split("pts_time:", 1)[1].strip().split()[0]
            timestamps.append(float(tok))
        except (ValueError, IndexError):
            continue

    # Pair timestamps with the actual files ffmpeg wrote.
    files = sorted(
        f for f in os.listdir(outdir) if f.startswith("scene_") and f.endswith(".jpg")
    )
    paired: list[tuple[float, str]] = []
    for idx, fname in enumerate(files):
        ts = timestamps[idx] if idx < len(timestamps) else 0.0
        paired.append((ts, os.path.join(outdir, fname)))
    return paired


def _even_interval_frames(
    video_path: str,
    *,
    outdir: str,
    timestamps: List[float],
    max_dim: int,
) -> List[Tuple[float, str]]:
    """Capture single frames at each timestamp via `-ss <t> -frames:v 1`.

    One ffmpeg invocation per timestamp keeps the seek precise; batched
    `-vf select=eq(n,…)` requires N total frame counts we can't predict
    cheaply. For 4-8 frames the overhead is negligible.
    """
    out: list[tuple[float, str]] = []
    for idx, ts in enumerate(timestamps):
        path = os.path.join(outdir, f"even_{idx:04d}.jpg")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-ss",
            f"{ts:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({max_dim},iw)':-1",
            "-q:v",
            "3",
            path,
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                timeout=120,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
            out.append((ts, path))
    return out


def _downsample(
    frames: List[Tuple[float, str]], target_count: int
) -> List[Tuple[float, str]]:
    """Pick the most-evenly-spaced `target_count` frames from `frames`.

    We sort by timestamp, then take indices linspaced over the list.
    Cheap and good enough; alternative is duration-aware spacing which
    requires duration to be known (we may not have it).
    """
    if target_count >= len(frames):
        return frames
    frames_sorted = sorted(frames, key=lambda x: x[0])
    step = (len(frames_sorted) - 1) / max(1, target_count - 1)
    picks = [frames_sorted[round(i * step)] for i in range(target_count)]
    return picks


def extract_keyframes(
    video_path: str,
    *,
    min_frames: int = DEFAULT_MIN_FRAMES,
    max_frames: int = DEFAULT_MAX_FRAMES,
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> List[ExtractedFrame]:
    """Top-level entry point. Returns `min_frames..max_frames` frames.

    Strategy:
      1. Scene-detect. If yield >= min and <= max → done.
      2. If yield < min: supplement with even-interval frames to reach min.
      3. If yield > max: downsample to max.

    Raises `FrameExtractError` only when ffmpeg is unusable or the
    input file is corrupt. Empty result (no frames at all) returns []
    so callers can decide whether that's fatal.
    """
    if not shutil.which("ffmpeg"):
        raise FrameExtractError("ffmpeg binary not found on PATH")
    if not os.path.exists(video_path):
        raise FrameExtractError(f"video file not found: {video_path}")

    duration = _probe_duration(video_path)

    with tempfile.TemporaryDirectory() as outdir:
        scene_frames = _scene_detect_frames(
            video_path,
            outdir=outdir,
            threshold=scene_threshold,
            max_dim=max_dimension,
        )

        if len(scene_frames) > max_frames:
            scene_frames = _downsample(scene_frames, max_frames)

        if len(scene_frames) < min_frames and duration > 0:
            # Supplement with even-spaced frames at timestamps not
            # already covered by scene-detect output.
            need = min_frames - len(scene_frames)
            # Generate `min_frames` evenly-spaced candidates over the
            # duration, then drop ones too close (<1s) to an existing
            # scene-detect frame.
            candidates = [
                duration * (i + 1) / (min_frames + 1) for i in range(min_frames)
            ]
            existing = [t for t, _ in scene_frames]
            fresh = [c for c in candidates if all(abs(c - e) > 1.0 for e in existing)][:need]
            if fresh:
                even = _even_interval_frames(
                    video_path,
                    outdir=outdir,
                    timestamps=fresh,
                    max_dim=max_dimension,
                )
                scene_frames.extend(even)
                scene_frames.sort(key=lambda x: x[0])

        # Edge case: scene-detect returned 0 and duration probe failed.
        # Fall back to fixed timestamps at 0%, 25%, 50%, 75% of an
        # assumed 10s — if the video is shorter, ffmpeg silently emits
        # nothing for out-of-range seeks, which we filter below.
        if not scene_frames:
            fallback_ts = [0.0, 2.5, 5.0, 7.5]
            scene_frames = _even_interval_frames(
                video_path,
                outdir=outdir,
                timestamps=fallback_ts,
                max_dim=max_dimension,
            )

        out: list[ExtractedFrame] = []
        for ts, path in scene_frames:
            try:
                with open(path, "rb") as fh:
                    out.append(ExtractedFrame(timestamp_sec=ts, jpeg_bytes=fh.read()))
            except OSError as exc:
                logger.warning(
                    "extracted_frame_read_failed", path=path, error=str(exc)
                )
        return out


def s3_key_for_frame(
    project_id: uuid.UUID, parent_asset_id: uuid.UUID, idx: int
) -> str:
    """Stable key shape. Idx is the in-order position of the frame in
    the extraction set; lets the panel sort thumbnails by appearance."""
    return (
        f"projects/{project_id}/brand-assets/"
        f"{parent_asset_id}-frame-{idx:02d}.jpg"
    )
