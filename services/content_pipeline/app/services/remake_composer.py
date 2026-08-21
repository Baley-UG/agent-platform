"""Compose a remake's shot clips into the final video.

Reuses `renderer.py`'s pure argv builders (concat, drawtext styles,
ducking, outro) via `compose_variant`, then adds the one thing the
scenario renderer never did: a logo overlay from the brand kit.

The shot clips arriving here are already normalized to the preset's
exact aspect/fps/codec by the `cut` / `normalize` steps, so compose is
the cheap concat-demuxer path.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from typing import List, Optional

from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.remake_shots import RemakeShot
from app.models.remakes import Remake
from app.services import renderer
from app.services.presets import PRESETS

_LOGO_POSITIONS = {
    "top_left": "{m}:{m}",
    "top_right": "W-w-{m}:{m}",
    "bottom_left": "{m}:H-h-{m}",
    "bottom_right": "W-w-{m}:H-h-{m}",
}


def _scene_texts(clips: List[RemakeShot]) -> List[renderer.SceneText]:
    """One on-screen line per clip that has a text_plan replacement.

    Phase 1 keeps it simple: the first replacement of a shot's text_plan
    becomes that scene's caption, windowed to the scene by scene_pos
    (the renderer computes the actual start/end from durations)."""
    out: List[renderer.SceneText] = []
    for pos, shot in enumerate(clips):
        plan = shot.text_plan or []
        if not plan:
            continue
        first = plan[0] if isinstance(plan[0], dict) else {}
        text = (first.get("replacement") or first.get("text") or "").strip()
        if not text:
            continue
        style = first.get("style") or "bold_white"
        out.append(renderer.SceneText(text=text, style=style, scene_pos=pos))
    return out


def _clip_durations(clips: List[RemakeShot]) -> List[float]:
    # Prefer the trimmed window; the cut clip matches it closely enough
    # for text windowing (the exact value is re-derived by the renderer).
    durs: List[float] = []
    for shot in clips:
        start = float(shot.trim_start_sec) if shot.trim_start_sec is not None else float(shot.start_sec)
        end = float(shot.trim_end_sec) if shot.trim_end_sec is not None else float(shot.end_sec)
        durs.append(max(end - start, 0.1))
    return durs


def _outro_key(session: Session, remake: Remake) -> Optional[str]:
    from app.models.templates import Template

    tpl_id = (remake.plan_json or {}).get("outro_template_id")
    if tpl_id:
        tpl = session.get(Template, uuid.UUID(str(tpl_id)))
        if tpl and tpl.video_s3_key:
            return tpl.video_s3_key
    # Fallback: newest project outro template.
    tpl = session.exec(
        select(Template)
        .where(Template.project_id == remake.project_id, Template.kind == "outro")
        .order_by(Template.created_at.desc())
    ).first()
    return tpl.video_s3_key if tpl and tpl.video_s3_key else None


def _logo_key(session: Session, remake: Remake) -> Optional[str]:
    from app.models.brand_kits import BrandKit

    kit = None
    if remake.brand_kit_id:
        kit = session.get(BrandKit, remake.brand_kit_id)
    if kit is None:
        kit = session.exec(
            select(BrandKit).where(BrandKit.project_id == remake.project_id, BrandKit.is_default == True)  # noqa: E712
        ).first()
    return kit.logo_s3_key if kit and kit.logo_s3_key else None


def _overlay_logo(remake: Remake, base_key: str, logo_key: str) -> str:
    """Second pass: burn the brand logo onto the composed video.

    Kept out of `build_compose_command` (which is scenario-shaped and
    heavily tested) — a small standalone ffmpeg pass is easier to reason
    about and only runs when a logo exists.
    """
    preset = PRESETS.get(remake.preset_key)
    width = preset.width if preset else 1080
    cfg = (remake.plan_json or {}).get("logo_overlay") or {}
    position = cfg.get("position", "top_right")
    scale = float(cfg.get("scale", 0.12))
    opacity = float(cfg.get("opacity", 0.85))
    margin = round(width * 0.04)
    xy = _LOGO_POSITIONS.get(position, _LOGO_POSITIONS["top_right"]).format(m=margin)

    work = tempfile.mkdtemp(prefix="logo-")
    try:
        base = os.path.join(work, "base.mp4")
        logo = os.path.join(work, "logo.png")
        out = os.path.join(work, "out.mp4")
        with open(base, "wb") as fh:
            s3lib.client().download_fileobj(settings.S3_BUCKET, base_key, fh)
        with open(logo, "wb") as fh:
            s3lib.client().download_fileobj(settings.S3_BUCKET, logo_key, fh)

        logo_w = round(width * scale)
        filt = (
            f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={opacity}[lg];"
            f"[0:v][lg]overlay=x={xy}:format=auto[vout]"
        )
        cmd = [
            "ffmpeg", "-hide_banner", "-y", "-loglevel", "warning",
            "-i", base, "-i", logo,
            "-filter_complex", filt,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", out,
        ]
        logger.info("remake_logo_overlay", cmd=" ".join(shlex.quote(a) for a in cmd))
        proc = subprocess.run(cmd, check=False, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            # Fail-open: ship without the logo rather than fail the remake.
            logger.warning("remake_logo_overlay_failed", stderr=(proc.stderr or "")[-500:])
            return base_key
        with open(out, "rb") as fh:
            data = fh.read()
        final_key = s3lib.make_key(remake.project_id, "finals", f"remake-{remake.id}-logo.mp4")
        s3lib.upload_bytes(final_key, data, content_type="video/mp4")
        return final_key
    finally:
        shutil.rmtree(work, ignore_errors=True)



def compose(session: Session, remake: Remake, clips: List[RemakeShot]) -> str:
    """Stitch shot clips → final video. Returns the S3 key.

    Versioned filename so a shot-reject recompose never overwrites the
    video the reviewer is currently watching.
    """
    audio_mode = (remake.plan_json or {}).get("audio_mode", "keep")
    inputs = renderer.ComposeInputs(
        scene_video_keys=[c.output_s3_key for c in clips],
        scene_durations_sec=_clip_durations(clips),
        scene_texts=_scene_texts(clips),
        source_audio_mode=audio_mode if audio_mode in ("keep", "duck", "drop") else "keep",
        outro_video_key=_outro_key(session, remake),
    )

    # A version counter keeps recompose outputs distinct.
    existing = remake.final_s3_key
    version = 1
    if existing and "_v" in existing:
        try:
            version = int(existing.rsplit("_v", 1)[1].split(".")[0]) + 1
        except (ValueError, IndexError):
            version = 2

    result = renderer.compose_variant(
        project_id=remake.project_id,
        scenario_id=remake.id,
        preset_key=remake.preset_key,
        inputs=inputs,
        output_filename=f"remake-{remake.id}_v{version}.mp4",
    )
    composed_key = result["s3_key"]

    logo_key = _logo_key(session, remake)
    if logo_key:
        return _overlay_logo(remake, composed_key, logo_key)
    return composed_key
