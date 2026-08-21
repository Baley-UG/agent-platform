"""The remake execution reconciler — the heart of the pipeline.

`advance(session, remake_id)` is idempotent and safe to call any number
of times, from anywhere (a worker finishing, an API action, the 60s
scheduler sweep). It derives everything from the DB rows:

  1. reap expired step leases (crashed worker / dropped message)
  2. enqueue every pending step whose dependencies are met
  3. derive each shot's status from its steps (pure, no set-equality)
  4. derive the remake's status (needs_attention is a flag, not a stop)
  5. cross phase boundaries (author_plan done → plan_review;
     compose done → final_review)

This replaces v1's fatal design where a single scalar status was
advanced by set-equality over child statuses and hung on any
heterogeneous mix. Here a failed step pauses only its own shot; the rest
keep rendering.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Dict, List, Optional

from sqlmodel import Session, select

from app.core.logging import logger
from app.models._base import utcnow
from app.models.remake_shots import RemakeShot
from app.models.remake_steps import STEP_QUEUES, RemakeStep
from app.models.remakes import REMAKE_FROZEN_STATUSES, Remake
from app.services import queue as queue_svc

# How long a step may sit queued/running before the sweep re-drives it.
# Generous — matches the queue timeouts plus slack for provider polling.
_LEASE_SECONDS: Dict[str, int] = {
    "probe": 300,
    "scene_detect": 900,
    "frame_extract": 900,
    "asr": 900,
    "tag_shots": 900,
    "author_plan": 900,
    "cut": 1800,
    "erase": 1200,
    "restyle": 1200,
    "keyframe_edit_start": 900,
    "keyframe_edit_end": 900,
    "i2v": 1200,
    "normalize": 1800,
    "tts": 600,
    "lipsync": 1200,
    "compose": 2400,
    "upscale": 1200,
}
_DEFAULT_LEASE = 900

# One worker entrypoint per queue; the worker dispatches on step.kind.
_WORKER_FOR_QUEUE = {
    "remake_ffmpeg": "app.workers.remake_ffmpeg.run",
    "remake_ai": "app.workers.remake_ai.run",
    "remake_analysis": "app.workers.remake_analysis.run",
}

# Steps pending longer than this are surfaced as stuck (metric + log).
_STUCK_AFTER = timedelta(minutes=30)


def _lease_for(kind: str) -> int:
    return _LEASE_SECONDS.get(kind, _DEFAULT_LEASE)


def _scope_key(step: RemakeStep):
    return step.shot_id  # None == global scope


def _deps_met(step: RemakeStep, by_scope: Dict[Optional[uuid.UUID], List[RemakeStep]],
              shots_ready: Dict[uuid.UUID, bool]) -> bool:
    """A step is runnable once every lower-seq step in its own scope has
    succeeded or been skipped.

    `compose` (global) additionally needs every non-dropped shot ready —
    the one genuine cross-scope dependency.
    """
    scope = by_scope.get(_scope_key(step), [])
    for other in scope:
        if other.id == step.id:
            continue
        if other.seq < step.seq and other.status not in ("succeeded", "skipped"):
            return False
    if step.kind == "compose":
        # Every shot that isn't dropped must have produced its output.
        if not shots_ready:
            return False
        return all(shots_ready.values())
    return True


def _derive_shot_status(shot: RemakeShot, steps: List[RemakeStep]) -> str:
    if shot.technique == "drop":
        return "dropped"
    if not steps:
        return "planned"
    statuses = [s.status for s in steps]
    if any(s == "failed" for s in statuses):
        return "needs_attention"
    if all(s in ("succeeded", "skipped") for s in statuses):
        return "ready"
    if any(s in ("queued", "running") for s in statuses):
        return "rendering"
    return "planned"


def advance(session: Session, remake_id: uuid.UUID) -> None:
    """Drive one remake forward by one reconciliation pass."""
    remake = session.exec(
        select(Remake).where(Remake.id == remake_id).with_for_update()
    ).first()
    if remake is None:
        return
    # Never cross a human gate or a terminal state on our own.
    if remake.status in REMAKE_FROZEN_STATUSES:
        return

    steps = list(session.exec(select(RemakeStep).where(RemakeStep.remake_id == remake_id)).all())
    shots = list(session.exec(select(RemakeShot).where(RemakeShot.remake_id == remake_id)).all())
    now = utcnow()

    # 1) Reap expired leases.
    for s in steps:
        if s.status in ("queued", "running") and s.lease_expires_at and s.lease_expires_at < now:
            s.attempts += 1
            s.status = "pending" if s.attempts < s.max_attempts else "failed"
            s.lease_expires_at = None
            if s.status == "failed" and not s.error:
                s.error = "step lease expired (worker crashed or dropped)"
            session.add(s)
    session.flush()

    # Group steps by scope + compute per-shot readiness for compose deps.
    by_scope: Dict[Optional[uuid.UUID], List[RemakeStep]] = {}
    for s in steps:
        by_scope.setdefault(_scope_key(s), []).append(s)

    shot_steps: Dict[uuid.UUID, List[RemakeStep]] = {sh.id: [] for sh in shots}
    for s in steps:
        if s.shot_id is not None and s.shot_id in shot_steps:
            shot_steps[s.shot_id].append(s)

    shots_ready: Dict[uuid.UUID, bool] = {}
    for sh in shots:
        if sh.technique == "drop":
            continue
        st = shot_steps.get(sh.id, [])
        shots_ready[sh.id] = bool(st) and all(x.status in ("succeeded", "skipped") for x in st)

    # 2) Enqueue runnable pending steps. Commit BEFORE enqueue so the
    #    worker always sees the row as queued; a failed enqueue rolls the
    #    row back to pending for the next sweep.
    for s in steps:
        if s.status != "pending" or not _deps_met(s, by_scope, shots_ready):
            continue
        s.status = "queued"
        s.lease_expires_at = now + timedelta(seconds=_lease_for(s.kind))
        session.add(s)
        session.commit()
        qname = STEP_QUEUES.get(s.kind, "remake_ai")
        func = _WORKER_FOR_QUEUE[qname]
        try:
            queue_svc.enqueue(qname, func, str(s.id), job_id=f"rmstep_{s.id}_{s.attempts}")
        except Exception as exc:  # noqa: BLE001 — Redis down: leave for the sweep
            s.status = "pending"
            s.lease_expires_at = None
            session.add(s)
            session.commit()
            logger.warning("remake_step_enqueue_failed", step_id=str(s.id), kind=s.kind, error=str(exc))

    # 3) Derive shot statuses.
    for sh in shots:
        new_status = _derive_shot_status(sh, shot_steps.get(sh.id, []))
        if sh.status != new_status:
            sh.status = new_status
            session.add(sh)

    # 4) Derive remake status.
    any_failed = any(s.status == "failed" for s in steps)
    author_plan = next((s for s in steps if s.kind == "author_plan"), None)
    compose = next((s for s in steps if s.kind == "compose"), None)

    new_remake_status = remake.status
    if remake.status == "analyzing":
        # Phase boundary: once the plan is authored, hand to the operator.
        if author_plan and author_plan.status == "succeeded" and shots:
            new_remake_status = "plan_review"
        elif any_failed:
            new_remake_status = "needs_attention"
    elif remake.status in ("rendering", "needs_attention"):
        if compose and compose.status == "succeeded":
            new_remake_status = "final_review"
        elif any_failed:
            new_remake_status = "needs_attention"
        else:
            new_remake_status = "rendering"

    if new_remake_status != remake.status:
        remake.status = new_remake_status
        session.add(remake)

    # 5) Stuck detection (observability only).
    for s in steps:
        if s.status == "pending" and s.updated_at and (now - s.updated_at) > _STUCK_AFTER:
            logger.warning("cp_remake_stuck", remake_id=str(remake_id), step_id=str(s.id), kind=s.kind)

    session.commit()
