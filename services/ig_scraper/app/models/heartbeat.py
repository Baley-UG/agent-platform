"""ig_worker_heartbeat — liveness tracking for worker and scheduler."""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class WorkerHeartbeat(SQLModel, table=True):
    """One row per long-running process.

    `process` is `worker` or `scheduler`. `/health` rejects rows whose
    `last_seen_at` is older than `IG_HEARTBEAT_STALE_AFTER_SECONDS`.
    """

    __tablename__ = "ig_worker_heartbeat"

    process: str = Field(primary_key=True)
    instance_id: str = Field(primary_key=True, description="Hostname / pod id; many replicas allowed.")
    last_seen_at: datetime = Field(default_factory=utcnow)
    started_at: datetime = Field(default_factory=utcnow)
    pid: Optional[int] = Field(default=None)
    version: Optional[str] = Field(default=None)
