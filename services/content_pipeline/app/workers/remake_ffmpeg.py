"""ffmpeg-bound remake steps — runs in the render container.

Handles: probe, scene_detect, frame_extract, cut, normalize, compose.
Each is a `Handler(session, step, remake) -> output dict`.
"""

from __future__ import annotations

import shutil
import uuid
from typing import Optional

from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.logging import logger
from app.models.remake_shots import RemakeShot
from app.models.remake_steps import RemakeStep
from app.models.remakes import Remake
from app.services import segment_cutter as cutter
from app.services import video_frames as vf
from app.workers import remake_common as common

_FRAME_KEY = "remakes/{remake_id}/shots/{idx:02d}/{pos}.jpg"


def _preset_aspect(preset_key: str) -> str:
    from app.services.presets import PRESETS

    p = PRESETS.get(preset_key)
    return p.aspect if p else "9:16"


def _probe(session: Session, step: RemakeStep, remake: Remake) -> dict:
    work = common.tempdir()
    try:
        src = common.download_to(work, remake.source_s3_key)
        meta = vf.probe_meta(src)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    remake.source_meta = meta
    remake.source_duration_sec = meta.get("duration_sec")
    session.add(remake)
    session.flush()
    return {"meta": meta}


def _scene_detect(session: Session, step: RemakeStep, remake: Remake) -> dict:
    work = common.tempdir()
    try:
        src = common.download_to(work, remake.source_s3_key)
        boundaries = vf.detect_scene_boundaries(src)
        duration = remake.source_duration_sec or vf._probe_duration(src) or 0.0
    finally:
        shutil.rmtree(work, ignore_errors=True)

    windows = vf.plan_shot_windows(boundaries, float(duration))
    if not windows:
        raise RuntimeError("could not derive any shot windows from the source video")

    # Idempotent: only create shots if none exist yet (a re-run of
    # scene_detect after a lease reap must not duplicate them).
    existing = session.exec(select(RemakeShot).where(RemakeShot.remake_id == remake.id)).all()
    if not existing:
        for i, (start, end) in enumerate(windows):
            session.add(
                RemakeShot(
                    remake_id=remake.id, idx=i, start_sec=start, end_sec=end,
                    technique="copy", status="planned",
                )
            )
        session.flush()
    return {"shots": len(windows), "boundaries": len(boundaries)}


def _frame_extract(session: Session, step: RemakeStep, remake: Remake) -> dict:
    shots = session.exec(
        select(RemakeShot).where(RemakeShot.remake_id == remake.id).order_by(RemakeShot.idx)
    ).all()
    if not shots:
        raise RuntimeError("frame_extract ran before scene_detect wrote shots")

    work = common.tempdir()
    try:
        src = common.download_to(work, remake.source_s3_key)
        for shot in shots:
            start, end = float(shot.start_sec), float(shot.end_sec)
            mid = (start + end) / 2
            frames: dict = {}
            for pos, t in (("start", start + 0.05), ("mid", mid), ("end", max(end - 0.1, start))):
                try:
                    data = vf.grab_frame(src, t)
                except vf.FrameExtractError as exc:
                    logger.warning("remake_frame_grab_failed", shot=shot.idx, pos=pos, error=str(exc))
                    continue
                key = _FRAME_KEY.format(remake_id=remake.id, idx=shot.idx, pos=pos)
                s3lib.upload_bytes(key, data, content_type="image/jpeg")
                frames[pos] = key
            shot.frames = frames
            session.add(shot)
        session.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"shots_framed": len(shots)}


def _shot_for_step(session: Session, step: RemakeStep) -> RemakeShot:
    shot = session.get(RemakeShot, step.shot_id) if step.shot_id else None
    if shot is None:
        raise RuntimeError(f"step {step.id} has no shot")
    return shot


def _cut(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """Cut this shot's window into a normalized clip. For `copy` this IS
    the shot output; for `erase` it's the clip the inpaint step consumes."""
    shot = _shot_for_step(session, step)
    aspect = _preset_aspect(remake.preset_key)
    fit_mode = (remake.plan_json or {}).get("fit_mode", "cover")

    start = float(shot.trim_start_sec) if shot.trim_start_sec is not None else float(shot.start_sec)
    end = float(shot.trim_end_sec) if shot.trim_end_sec is not None else float(shot.end_sec)
    cut = cutter.Cut(idx=shot.idx, start_sec=start, end_sec=end)

    results = cutter.cut_segments(
        project_id=remake.project_id,
        scenario_id=remake.id,  # key namespace param; remake id is fine
        src_s3_key=remake.source_s3_key,
        segments=[cut],
        aspect=aspect,
        fit_mode=fit_mode,
    )
    if not results:
        raise RuntimeError("cut produced no output")
    out = results[0]
    if shot.technique == "copy":
        shot.output_s3_key = out["s3_key"]
        session.add(shot)
        session.flush()
    return {"s3_key": out["s3_key"], "duration_sec": out["duration_sec"]}


def _normalize(session: Session, step: RemakeStep, remake: Remake) -> dict:
    """Conform an AI-produced clip (erase/i2v output) back to the preset's
    exact aspect/fps/codec, then stamp it as the shot output."""
    shot = _shot_for_step(session, step)
    src_key = common.prev_output_key(session, step)
    if not src_key:
        raise RuntimeError("normalize has no upstream clip to conform")
    aspect = _preset_aspect(remake.preset_key)
    fit_mode = (remake.plan_json or {}).get("fit_mode", "cover")

    # Re-run the single-cut path over the whole AI clip (start=0..dur).
    work = common.tempdir()
    try:
        local = common.download_to(work, src_key)
        dur = vf._probe_duration(local) or 0.0
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if dur <= 0:
        raise RuntimeError("normalize could not probe the upstream clip duration")

    results = cutter.cut_segments(
        project_id=remake.project_id,
        scenario_id=remake.id,
        src_s3_key=src_key,
        segments=[cutter.Cut(idx=shot.idx, start_sec=0.0, end_sec=dur)],
        aspect=aspect,
        fit_mode=fit_mode,
    )
    if not results:
        raise RuntimeError("normalize produced no output")
    shot.output_s3_key = results[0]["s3_key"]
    session.add(shot)
    session.flush()
    return {"s3_key": results[0]["s3_key"], "duration_sec": results[0]["duration_sec"]}


def _compose(session: Session, step: RemakeStep, remake: Remake) -> dict:
    from app.services import remake_composer

    shots = session.exec(
        select(RemakeShot).where(RemakeShot.remake_id == remake.id).order_by(RemakeShot.idx)
    ).all()
    clips = [s for s in shots if s.technique != "drop" and s.output_s3_key]
    if not clips:
        raise RuntimeError("compose has no shot clips to stitch")
    final_key = remake_composer.compose(session, remake, clips)
    remake.final_s3_key = final_key
    session.add(remake)
    session.flush()
    return {"s3_key": final_key}


_HANDLERS = {
    "probe": _probe,
    "scene_detect": _scene_detect,
    "frame_extract": _frame_extract,
    "cut": _cut,
    "normalize": _normalize,
    "compose": _compose,
}


def run(step_id: str) -> dict:
    return common.run_step(step_id, _HANDLERS)
