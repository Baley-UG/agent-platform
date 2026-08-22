"""Shared plumbing for the remake step workers.

Every remake worker is `run(step_id)`: load the step, do its work,
record success/failure, then call the reconciler. The retry/failure
bookkeeping is identical across all three queues, so it lives here.

A worker NEVER enqueues another worker and NEVER touches another row's
status — it only advances its own step and then asks the reconciler to
decide what runs next. That single rule is what keeps the pipeline a
graph instead of v1's fragile chain.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from typing import Callable, Optional

from sqlmodel import Session, select

from app.core import s3 as s3lib
from app.core.config import settings
from app.core.logging import logger
from app.models.remake_steps import RemakeStep
from app.models.remakes import Remake
from app.services import remake_reconciler
from app.services.database import session_scope

# A handler does the actual work and returns the step's `output` dict
# (e.g. {"s3_key": ...}). Raising signals failure → retry/fail bookkeeping.
Handler = Callable[[Session, RemakeStep, Remake], Optional[dict]]


def run_step(step_id: str, handlers: dict[str, Handler]) -> dict:
    """Execute one step by dispatching on its kind.

    `handlers` maps `step.kind` → handler. Unknown kinds are a config
    error (a step was authored for a queue that can't run it).
    """
    step_uuid = uuid.UUID(step_id)
    with session_scope() as session:
        step = session.get(RemakeStep, step_uuid)
        if step is None:
            return {"ok": False, "error": "step not found"}
        remake = session.get(Remake, step.remake_id)
        if remake is None:
            return {"ok": False, "error": "remake not found"}

        handler = handlers.get(step.kind)
        if handler is None:
            step.status = "failed"
            step.error = f"no handler for kind={step.kind} on this queue"
            session.add(step)
            session.commit()
            return {"ok": False, "error": step.error}

        step.status = "running"
        session.add(step)
        session.commit()

        try:
            output = handler(session, step, remake)
        except Exception as exc:  # noqa: BLE001 — retry/fail bookkeeping
            step.attempts += 1
            step.lease_expires_at = None
            if step.attempts < step.max_attempts:
                step.status = "pending"  # the sweep / this advance re-drives it
            else:
                step.status = "failed"
            step.error = str(exc)[:2000]
            session.add(step)
            session.commit()
            logger.warning("remake_step_failed", step_id=step_id, kind=step.kind, attempts=step.attempts, error=str(exc))
            remake_reconciler.advance(session, step.remake_id)
            return {"ok": False, "error": str(exc), "attempts": step.attempts}

        step.status = "succeeded"
        step.error = None
        step.lease_expires_at = None
        if output is not None:
            step.output = output
        session.add(step)
        session.commit()

        remake_reconciler.advance(session, step.remake_id)
        return {"ok": True, "step_id": step_id, "kind": step.kind}


# ---------------------------------------------------------------------------
# helpers shared by handlers
# ---------------------------------------------------------------------------


def download_to(dest_dir: str, s3_key: str, filename: str = "in.mp4") -> str:
    """Download an S3 object into a local path. Caller owns dest_dir."""
    local = os.path.join(dest_dir, filename)
    with open(local, "wb") as fh:
        s3lib.client().download_fileobj(settings.S3_BUCKET, s3_key, fh)
    return local


def prev_output_key(session: Session, step: RemakeStep) -> Optional[str]:
    """The `output.s3_key` of the highest-seq succeeded step in this
    step's shot scope below its own seq — i.e. what this step consumes."""
    if step.shot_id is None:
        return None
    rows = session.exec(
        select(RemakeStep).where(
            RemakeStep.shot_id == step.shot_id,
            RemakeStep.seq < step.seq,
            RemakeStep.status == "succeeded",
        ).order_by(RemakeStep.seq.desc())
    ).all()
    for r in rows:
        if r.output and r.output.get("s3_key"):
            return r.output["s3_key"]
    return None


def tempdir(prefix: str = "remake-") -> str:
    return tempfile.mkdtemp(prefix=prefix)


def shot_step_output(session: Session, shot_id, kind: str) -> Optional[str]:
    """The `output.s3_key` of a specific succeeded step of a shot.

    Used where `prev_output_key` (highest-seq) is ambiguous — e.g. the
    reframe `i2v` step needs BOTH its start and end keyframe edits, which
    share a seq, so it resolves them by kind rather than by seq order.
    """
    row = session.exec(
        select(RemakeStep).where(
            RemakeStep.shot_id == shot_id,
            RemakeStep.kind == kind,
            RemakeStep.status == "succeeded",
        )
    ).first()
    if row and row.output and row.output.get("s3_key"):
        return row.output["s3_key"]
    return None
