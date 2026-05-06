"""/health and /ready endpoints.

`/health` is publicly accessible (used by Docker / k8s probes).
`/ready` covers DB + worker + scheduler heartbeat freshness and is what
operators actually look at.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger
from app.services.database import read_session_scope, session_scope

router = APIRouter()


def _heartbeat_fresh(process: str) -> bool:
    """Return True if `process` has reported a heartbeat recently."""
    threshold = datetime.now(timezone.utc) - timedelta(seconds=settings.IG_HEARTBEAT_STALE_AFTER_SECONDS)
    try:
        with read_session_scope() as session:
            row = session.exec(
                text(
                    "SELECT MAX(last_seen_at) AS ts FROM ig_worker_heartbeat WHERE process = :p"
                ).bindparams(p=process)
            ).first()
            ts = row[0] if row else None
            return bool(ts and ts >= threshold)
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat_check_failed", process=process, error=str(exc))
        return False


@router.get("/health")
async def health() -> Dict[str, Any]:
    """Liveness probe — process is up."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — DB reachable, worker + scheduler heartbeats fresh."""
    db_ok = False
    try:
        with session_scope() as session:
            session.exec(text("SELECT 1"))
            db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ready_db_check_failed", error=str(exc))

    worker_ok = _heartbeat_fresh("worker")
    scheduler_ok = _heartbeat_fresh("scheduler")
    overall = db_ok and worker_ok and scheduler_ok

    response = {
        "status": "ready" if overall else "degraded",
        "components": {
            "db": "ok" if db_ok else "down",
            "worker": "ok" if worker_ok else "stale",
            "scheduler": "ok" if scheduler_ok else "stale",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    code = status.HTTP_200_OK if overall else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(content=response, status_code=code)
