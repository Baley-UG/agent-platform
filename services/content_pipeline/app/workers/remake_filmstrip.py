"""Server-side filmstrip generation for the remake compare view.

Extracts one frame per second (capped) from the source reference video
and the composed final via ffmpeg, mirrors the JPEGs to S3 and records
the keys in `remake.plan_json["filmstrip"]`. The panel then renders the
editor-style timeline from plain <img> URLs — no client-side canvas
capture, so it works regardless of the bucket's CORS configuration.

Out-of-band job (NOT a remake_step): it runs after the pipeline is done
(final_review/done), never blocks a gate, and is safe to re-run — keys
are deterministic per remake, so a regeneration overwrites in place.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import structlog

from app.core import s3 as s3lib
from app.models.remakes import Remake
from app.services.database import session_scope
from app.workers import remake_common as common

logger = structlog.get_logger()

# One frame per second, at most this many cells per strip (matches the
# panel's 60-cell cap for long sources).
_MAX_FRAMES = 60
_THUMB_HEIGHT = 96
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _extract(video_path: str, out_dir: str, tag: str) -> list[str]:
    """fps=1 JPEG strip for one video; returns local paths in order."""
    pattern = os.path.join(out_dir, f"{tag}-%03d.jpg")
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-vf", f"fps=1,scale=-2:{_THUMB_HEIGHT}",
        "-frames:v", str(_MAX_FRAMES),
        "-q:v", "5",
        pattern,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    frames = sorted(
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.startswith(f"{tag}-") and f.endswith(".jpg")
    )
    return frames


def _upload_strip(remake: Remake, local_paths: list[str], tag: str) -> list[str]:
    keys: list[str] = []
    for i, path in enumerate(local_paths):
        key = f"projects/{remake.project_id}/remakes/{remake.id}/filmstrip/{tag}-{i:03d}.jpg"
        with open(path, "rb") as fh:
            s3lib.upload_bytes(key, fh.read(), content_type="image/jpeg")
        keys.append(key)
    return keys


def run(remake_id: str) -> dict:
    """RQ task: `app.workers.remake_filmstrip.run` on `remake_ffmpeg`."""
    with session_scope() as session:
        remake = session.get(Remake, remake_id)
        if remake is None:
            return {"skipped": "remake not found"}
        if not remake.final_s3_key:
            return {"skipped": "no final video"}

        filmstrip: dict[str, list[str]] = {}
        with tempfile.TemporaryDirectory() as work:
            final_local = common.download_to(work, remake.final_s3_key, "final.mp4")
            filmstrip["final"] = _upload_strip(remake, _extract(final_local, work, "fin"), "fin")

            src_key = remake.source_s3_key or ""
            # Image-only references have no source strip.
            if src_key and not src_key.lower().endswith(_IMAGE_EXTS) and src_key != remake.final_s3_key:
                try:
                    src_local = common.download_to(work, src_key, "source.mp4")
                    filmstrip["source"] = _upload_strip(remake, _extract(src_local, work, "src"), "src")
                except Exception:  # noqa: BLE001 — a broken source mirror shouldn't kill the final strip
                    logger.warning("filmstrip_source_failed", remake_id=str(remake.id))

        plan = dict(remake.plan_json or {})
        plan["filmstrip"] = filmstrip
        remake.plan_json = plan
        session.add(remake)
        session.commit()
        logger.info(
            "filmstrip_generated",
            remake_id=str(remake.id),
            final_frames=len(filmstrip.get("final", [])),
            source_frames=len(filmstrip.get("source", [])),
        )
        return {"final": len(filmstrip.get("final", [])), "source": len(filmstrip.get("source", []))}
