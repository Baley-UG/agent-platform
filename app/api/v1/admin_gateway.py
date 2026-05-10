"""Hybrid gateway — proxies admin requests to content_pipeline + ig_scraper.

Admin panel calls:
  /api/v1/cp/{path}        → CONTENT_PIPELINE_URL/api/v1/{path}
  /api/v1/scraper/{path}   → IG_SCRAPER_URL/api/v1/{path}

Auth: every request must carry a valid Bearer admin JWT. The middleware
also enforces project-membership when a `/projects/{pid}/...` path
segment is present (members can access; admins always pass).

Downstream services receive `X-API-Key` (their service token) — they
never see the user's JWT or know which user made the call. Audit, if
ever needed, lives in main app.

Hybrid intent: this generic proxy covers 95% of CRUD; specific
business-logic endpoints can be hand-rolled in `admin_users.py`,
`admin_auth.py`, etc, and shadow the proxy automatically because
FastAPI registers explicit routes before catch-alls.
"""

from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.v1.admin_deps import AdminPrincipal, require_admin_token
from app.core.config import settings
from app.core.logging import logger
from app.services import admin_auth_service as svc
from app.services.database import database_service
from sqlmodel import Session


router = APIRouter(tags=["gateway"])

_PROJECT_RE = re.compile(r"^projects/(?P<pid>[0-9a-fA-F-]{36})(/|$)")
_FORWARD_TIMEOUT = httpx.Timeout(120.0)
_HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "expect",
}


def _maybe_extract_project_id(path: str) -> Optional[UUID]:
    """Return the project UUID from path forms like `projects/<uuid>/...`."""
    m = _PROJECT_RE.match(path.lstrip("/"))
    if not m:
        return None
    try:
        return UUID(m.group("pid"))
    except ValueError:
        return None


def _check_project_access(principal: AdminPrincipal, project_id: UUID) -> None:
    """Project-scope enforcement at the gateway layer.

    Admin role bypasses; non-admins need any membership row. 404 (not 403)
    on missing membership so we don't leak existence.
    """
    if principal.role == "admin":
        return
    with Session(database_service.engine) as session:
        if svc.get_membership(session, principal.user.id, project_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")


async def _proxy(
    *,
    request: Request,
    base_url: str,
    api_key: str,
    path: str,
) -> Response:
    """Forward `request` to `<base_url>/api/v1/<path>` with X-API-Key."""
    url = f"{base_url.rstrip('/')}/api/v1/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() != "authorization"
    }
    forward_headers["X-API-Key"] = api_key
    forward_headers["X-Forwarded-By"] = "agent-platform-gateway"

    try:
        async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT) as client:
            upstream = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=forward_headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("gateway_upstream_error", url=url, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"upstream error: {exc}"
        ) from exc

    response_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


# ---- /api/v1/cp/{path} → content_pipeline ----


@router.api_route(
    "/cp/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_content_pipeline(
    path: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> Response:
    """Proxy to the content_pipeline microservice."""
    if not settings.CONTENT_PIPELINE_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="content_pipeline service token not configured (CP_API_KEY)",
        )
    project_id = _maybe_extract_project_id(path)
    if project_id is not None:
        _check_project_access(principal, project_id)
    return await _proxy(
        request=request,
        base_url=settings.CONTENT_PIPELINE_URL,
        api_key=settings.CONTENT_PIPELINE_API_KEY,
        path=path,
    )


# ---- /api/v1/scraper/{path} → ig_scraper (admin-only) ----


@router.api_route(
    "/scraper/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_ig_scraper(
    path: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> Response:
    """Proxy to ig_scraper. Global admin only — scraper has no per-project scope."""
    if principal.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    if not settings.IG_SCRAPER_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ig_scraper service token not configured (IG_SCRAPER_API_KEY)",
        )
    return await _proxy(
        request=request,
        base_url=settings.IG_SCRAPER_URL,
        api_key=settings.IG_SCRAPER_API_KEY,
        path=path,
    )
