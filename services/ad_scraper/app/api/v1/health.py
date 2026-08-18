"""/health and /ready endpoints.

`/health` is a liveness probe (Docker / k8s). `/ready` is what an operator
actually looks at: it reports DB reachability and — the thing that
silently breaks this service — whether a usable YouCloud session exists.

A dead credential does NOT make `/ready` return 503. The API is perfectly
able to serve already-ingested data without one; only new jobs are
blocked. It is surfaced as a warning field so a dashboard can alert on it
without taking the container out of rotation.
"""

from typing import Any, Dict

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger
from app.models.credential import ACTIVE, LOGIN_FAILED
from app.services import credentials as creds
from app.services.database import session_scope

router = APIRouter()


@router.get("/health")
async def health() -> Dict[str, Any]:
    """Liveness probe — process is up."""
    return {"status": "ok", "service": settings.PROJECT_NAME, "version": settings.VERSION}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — DB reachable; session state reported as a warning."""
    db_ok = False
    try:
        with session_scope() as session:
            session.exec(text("SELECT 1"))
            db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ready_db_check_failed", error=str(exc))

    session_state = "unknown"
    if db_ok:
        try:
            with session_scope() as session:
                row = creds.get_credential(session) or creds.pick_usable(session)
            if row is None or not row.session_cookie_enc:
                session_state = "missing"
            elif row.status == LOGIN_FAILED:
                session_state = "locked_out"
            elif creds.needs_refresh(row):
                session_state = "expiring"
            elif row.status == ACTIVE:
                session_state = "active"
            else:
                session_state = row.status
        except Exception as exc:  # noqa: BLE001 — pre-migration DB is fine here
            logger.warning("ready_credential_check_failed", error=str(exc))
            session_state = "unavailable"

    response: Dict[str, Any] = {
        "status": "ready" if db_ok else "not_ready",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "checks": {"database": db_ok},
        # Not part of readiness on purpose — see the module docstring.
        "youcloud_session": session_state,
    }
    if session_state not in ("active", "unknown"):
        response["warning"] = (
            "no usable YouCloud session — new ingestion jobs will fail. "
            "PUT /api/v1/credentials/session with a fresh sessionId cookie."
        )

    code = status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=response)
