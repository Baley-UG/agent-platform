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


DEFAULT_BOUNDARY_THRESHOLD = 0.22


def detect_scene_boundaries(
    video_path: str, *, threshold: float = DEFAULT_BOUNDARY_THRESHOLD
) -> List[float]:
    """Return the source video's shot-boundary timestamps, sorted.

    `extract_keyframes` mixes true scene-detect hits with even-interval
    filler and caps the count, so its timestamps are a thumbnail set,
    not a cut list. Repurpose mode needs the real boundaries: same
    `select='gt(scene,T)'` + `showinfo` parse as `_scene_detect_frames`,
    but written to the null muxer (no jpegs) with no count cap and a
    lower threshold, so softer cuts survive.

    0.0 and EOF are excluded — `segments.plan_segments` supplies those.
    Returns [] when ffmpeg is missing or the probe fails; callers fall
    back to even intervals.
    """
    if not shutil.which("ffmpeg"):
        return []
    if not os.path.exists(video_path):
        return []

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        video_path,
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f",
        "null",
        "-",
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
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("scene_boundary_detect_failed", error=str(exc), path=video_path)
        return []
    if proc.returncode != 0:
        tail = (proc.stderr or "").splitlines()[-6:]
        logger.warning(
            "scene_boundary_detect_nonzero",
            code=proc.returncode,
            stderr=" | ".join(tail),
        )
        return []

    out: list[float] = []
    for line in (proc.stderr or "").splitlines():
        if "pts_time:" not in line:
            continue
        try:
            tok = line.split("pts_time:", 1)[1].strip().split()[0]
            ts = float(tok)
        except (ValueError, IndexError):
            continue
        if ts > 0.0:
            out.append(ts)
    return sorted(set(out))


# Shot-window planning — the cut list for a remake. Clamps tuned for
# short-form ads; Kling O1 caps v2v input at 10s so long shots split.
MIN_SHOT_SEC = 1.5
MAX_SHOT_SEC = 8.0
MAX_SHOTS = 14


def plan_shot_windows(
    boundaries: List[float],
    duration: float,
    *,
    min_sec: float = MIN_SHOT_SEC,
    max_sec: float = MAX_SHOT_SEC,
    max_count: int = MAX_SHOTS,
) -> List[Tuple[float, float]]:
    """Turn detected shot boundaries + duration into `(start, end)` windows.

    Merge windows shorter than `min_sec` into their predecessor (a burst
    of rapid cuts collapses into one usable shot), split windows longer
    than `max_sec`, then cap the count by merging the shortest neighbours.
    Pure — unit-testable without ffmpeg.
    """
    if duration <= 0:
        return []
    inner = sorted({round(b, 3) for b in boundaries if 0.0 < b < duration})
    bounds = [0.0, *inner, round(duration, 3)]

    # merge shorts (tail merges backwards)
    merged = [bounds[0]]
    for b in bounds[1:-1]:
        if b - merged[-1] >= min_sec:
            merged.append(b)
    merged.append(bounds[-1])
    while len(merged) > 2 and merged[-1] - merged[-2] < min_sec:
        del merged[-2]

    # split longs
    split: List[float] = [merged[0]]
    for prev, cur in zip(merged, merged[1:]):
        span = cur - prev
        if span > max_sec:
            pieces = int(span // max_sec) + 1
            step = span / pieces
            split.extend(prev + step * i for i in range(1, pieces))
        split.append(cur)

    # cap count
    while len(split) - 1 > max_count:
        spans = [(split[i + 1] - split[i], i) for i in range(len(split) - 1)]
        _, shortest = min(spans)
        drop = shortest + 1 if shortest + 1 < len(split) - 1 else shortest
        del split[drop]

    return [(round(split[i], 3), round(split[i + 1], 3)) for i in range(len(split) - 1)]


def grab_frame(video_path: str, timestamp_sec: float, *, max_dim: int = DEFAULT_MAX_DIMENSION) -> bytes:
    """Grab a single JPEG frame at `timestamp_sec`. Raises on failure."""
    if not shutil.which("ffmpeg"):
        raise FrameExtractError("ffmpeg binary not found on PATH")
    with tempfile.TemporaryDirectory() as outdir:
        out = os.path.join(outdir, "frame.jpg")
        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-ss", f"{max(timestamp_sec, 0.0):.3f}",
            "-i", video_path, "-frames:v", "1",
            "-vf", f"scale='min({max_dim},iw)':-1", "-q:v", "3", out,
        ]
        proc = subprocess.run(cmd, check=False, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            tail = (proc.stderr or b"").decode("utf-8", "ignore").splitlines()[-4:]
            raise FrameExtractError(f"grab_frame failed at {timestamp_sec}s: {' | '.join(tail)}")
        with open(out, "rb") as fh:
            return fh.read()


def probe_meta(video_path: str) -> dict:
    """ffprobe → {duration_sec, width, height, fps, has_audio}."""
    if not shutil.which("ffprobe"):
        return {"duration_sec": _probe_duration(video_path)}
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height,avg_frame_rate",
                "-of", "json", video_path,
            ],
            stderr=subprocess.PIPE, timeout=30,
        )
        data = json.loads(out)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return {"duration_sec": _probe_duration(video_path)}
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    fps = 0.0
    rate = video.get("avg_frame_rate") or "0/1"
    try:
        num, den = rate.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration_sec": float(fmt.get("duration") or 0.0),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "has_audio": has_audio,
    }


def s3_key_for_frame(
    project_id: uuid.UUID, parent_asset_id: uuid.UUID, idx: int
) -> str:
    """Stable key shape. Idx is the in-order position of the frame in
    the extraction set; lets the panel sort thumbnails by appearance."""
    return (
        f"projects/{project_id}/brand-assets/"
        f"{parent_asset_id}-frame-{idx:02d}.jpg"
    )
