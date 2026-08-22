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
# CRITICAL: every lease must be comfortably LARGER than the handler's own
# timeout, or a healthy-but-slow job gets reaped mid-run — burning a
# retry and risking a double (paid) provider call. These are ~2× the
# provider/subprocess timeouts in the workers.
_LEASE_SECONDS: Dict[str, int] = {
    "probe": 600,
    "scene_detect": 1800,
    "frame_extract": 1800,
    "asr": 1800,          # handler timeout 900
    "tag_shots": 1200,
    "author_plan": 1200,
    "cut": 3600,          # ffmpeg subprocess timeout 1800
    "erase": 2400,        # fal timeout 1200
    "restyle": 2400,      # fal timeout 1200
    "keyframe_edit_start": 1200,   # fal timeout 600
    "keyframe_edit_end": 1200,
    "i2v": 2400,          # fal timeout 1200
    "normalize": 3600,
    "tts": 1200,
    "lipsync": 2400,
    "compose": 3600,
    "upscale": 2400,
}
_DEFAULT_LEASE = 1800

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


def _acquire_lock(session: Session, remake_id: uuid.UUID) -> None:
    """Serialize concurrent advance() calls for one remake.

    A transaction-scoped Postgres advisory lock held for the WHOLE pass
    (released only at the single commit at the end). This replaces the
    old `SELECT ... FOR UPDATE`, which was released by the mid-loop
    commits and let a worker's advance() interleave with the sweep's —
    double-enqueuing the same step. No-op on non-Postgres (unit tests).
    """
    from sqlalchemy import text

    bind = session.get_bind()
    if getattr(bind.dialect, "name", "") != "postgresql":
        return
    key = int.from_bytes(remake_id.bytes[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


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
    """Drive one remake forward by one reconciliation pass.

    The whole pass runs in ONE transaction under an advisory lock, then
    a single commit, then enqueue. Enqueue AFTER commit is safe because
    the worker fences on `status == "queued"` (see remake_common.run_step)
    and the job_id dedups duplicate deliveries; a failed enqueue leaves
    the row `queued` with a lease that the sweep reaps and re-drives.
    """
    _acquire_lock(session, remake_id)

    remake = session.get(Remake, remake_id)
    if remake is None or remake.status in REMAKE_FROZEN_STATUSES:
        session.commit()  # release the advisory lock
        return

    steps = list(session.exec(select(RemakeStep).where(RemakeStep.remake_id == remake_id)).all())
    shots = list(session.exec(select(RemakeShot).where(RemakeShot.remake_id == remake_id)).all())
    now = utcnow()

    # 1) Reap expired leases (dead/dropped workers). Leases are >> handler
    #    timeouts, so a healthy job is never reaped mid-run.
    for s in steps:
        if s.status in ("queued", "running") and s.lease_expires_at and s.lease_expires_at < now:
            s.attempts += 1
            s.status = "pending" if s.attempts < s.max_attempts else "failed"
            s.lease_expires_at = None
            if s.status == "failed" and not s.error:
                s.error = "step lease expired (worker crashed or dropped)"
            session.add(s)

    by_scope: Dict[Optional[uuid.UUID], List[RemakeStep]] = {}
    for s in steps:
        by_scope.setdefault(_scope_key(s), []).append(s)

    shot_steps: Dict[uuid.UUID, List[RemakeStep]] = {sh.id: [] for sh in shots}
    for s in steps:
        if s.shot_id is not None and s.shot_id in shot_steps:
            shot_steps[s.shot_id].append(s)

    non_drop = [sh for sh in shots if sh.technique != "drop"]
    shots_ready: Dict[uuid.UUID, bool] = {}
    for sh in non_drop:
        st = shot_steps.get(sh.id, [])
        shots_ready[sh.id] = bool(st) and all(x.status in ("succeeded", "skipped") for x in st)

    # 2) Mark runnable pending steps queued (collect for post-commit enqueue).
    to_enqueue: List[tuple] = []
    for s in steps:
        if s.status != "pending" or not _deps_met(s, by_scope, shots_ready):
            continue
        s.status = "queued"
        s.lease_expires_at = now + timedelta(seconds=_lease_for(s.kind))
        session.add(s)
        qname = STEP_QUEUES.get(s.kind, "remake_ai")
        to_enqueue.append((qname, _WORKER_FOR_QUEUE[qname], str(s.id), f"rmstep_{s.id}_{s.attempts}"))

    # 3) Derive shot statuses.
    for sh in shots:
        new_status = _derive_shot_status(sh, shot_steps.get(sh.id, []))
        if sh.status != new_status:
            sh.status = new_status
            session.add(sh)

    # 4) Derive remake status — PHASE-AWARE. `plan_approved_at` is the
    #    phase signal: unset → analysis phase, set → render phase. Without
    #    this, an analysis-stage failure flips status to needs_attention
    #    and the next pass would wrongly take the render branch (and could
    #    skip Gate 1 entirely).
    any_failed = any(s.status == "failed" for s in steps)
    author_plan = next((s for s in steps if s.kind == "author_plan"), None)
    compose = next((s for s in steps if s.kind == "compose"), None)
    render_phase = remake.plan_approved_at is not None

    new_status = remake.status
    if not render_phase:
        # Analysis phase.
        if author_plan and author_plan.status == "succeeded" and shots:
            new_status = "plan_review"
        elif any_failed:
            new_status = "needs_attention"
            if not remake.error:
                failed = next((s for s in steps if s.status == "failed"), None)
                remake.error = f"analysis step '{failed.kind}' failed: {(failed.error or '')[:300]}" if failed else "analysis failed"
        else:
            new_status = "analyzing"
    else:
        # Render phase.
        if compose and compose.status == "succeeded":
            new_status = "final_review"
        elif not non_drop:
            # All shots dropped — nothing to compose. Defensive; also
            # guarded at approve_plan.
            new_status = "needs_attention"
            remake.error = "every shot is dropped — nothing to render"
        elif any_failed:
            new_status = "needs_attention"
        else:
            new_status = "rendering"

    if new_status != remake.status:
        remake.status = new_status
        session.add(remake)

    # 5) Stuck detection (observability only).
    for s in steps:
        if s.status == "pending" and s.updated_at and (now - s.updated_at) > _STUCK_AFTER:
            logger.warning("cp_remake_stuck", remake_id=str(remake_id), step_id=str(s.id), kind=s.kind)

    # Single commit ends the transaction and releases the advisory lock.
    session.commit()

    # 6) Enqueue AFTER commit. The worker fences on status=="queued" and
    #    the job_id dedups; a failed enqueue leaves the row queued for the
    #    sweep's lease reap.
    for qname, func, step_id, job_id in to_enqueue:
        try:
            queue_svc.enqueue(qname, func, step_id, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 — Redis down: the sweep re-drives
            logger.warning("remake_step_enqueue_failed", step_id=step_id, kind=qname, error=str(exc))
