"""Heartbeat persistence for worker and scheduler processes.

`/ready` checks the freshness of these rows; if no row for a process has
been seen in IG_HEARTBEAT_STALE_AFTER_SECONDS, readiness flips to
degraded.

We use `ON CONFLICT (process, instance_id) DO UPDATE SET last_seen_at`
so a single insert keeps both first-write and steady-state cheap.
"""

import os
import socket
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.logging import logger


def make_instance_id(suffix: Optional[str] = None) -> str:
    """Build a stable id for this process: hostname[:suffix]:pid."""
    host = socket.gethostname()
    if suffix:
        return f"{host}:{suffix}:{os.getpid()}"
    return f"{host}:{os.getpid()}"


_UPSERT = text(
    """
    INSERT INTO ig_worker_heartbeat
        (process, instance_id, last_seen_at, started_at, pid, version)
    VALUES
        (:process, :instance_id, now(), now(), :pid, :version)
    ON CONFLICT (process, instance_id) DO UPDATE
        SET last_seen_at = excluded.last_seen_at
    """
)


def beat(session: Session, process: str, instance_id: str) -> None:
    """Touch the heartbeat row for (process, instance_id).

    Errors are caught and logged; a transient DB blip should not crash
    the worker. The worker loop will keep retrying on the next interval.
    """
    try:
        session.execute(
            _UPSERT,
            {
                "process": process,
                "instance_id": instance_id,
                "pid": os.getpid(),
                "version": settings.VERSION,
            },
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.warning("heartbeat_failed", process=process, error=str(exc))
