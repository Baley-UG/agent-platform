"""Hybrid gateway — proxies admin requests to content_pipeline + ig_scraper.

Admin panel calls:
  /api/v1/cp/{path}                  → CONTENT_PIPELINE_URL/api/v1/{path}
  /api/v1/instagram-scraper/{path}   → IG_SCRAPER_URL/api/v1/{path}

Naming convention: `<platform>-<function>` for downstream proxies. The
`-scraper` suffix is deliberate — future Instagram operations that are
not scraping (e.g. Graph-API publishing on content_pipeline) get
`/api/v1/instagram-publisher/...` and the two paths can coexist without
ambiguity. TikTok scraping would similarly land at
`/api/v1/tiktok-scraper/...`, separate from `tiktok-ads-mcp`.

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
    # httpx already decodes the body before we forward it; passing
    # `content-encoding: gzip` along with the decoded bytes corrupts
    # the response for the browser.
    "content-encoding",
}

# Reused across requests for connection pooling. httpx clients are
# safe to share — they own a connection pool and a thread-safe lock.
_HTTPX: Optional[httpx.AsyncClient] = None


def _httpx_client() -> httpx.AsyncClient:
    global _HTTPX
    if _HTTPX is None or _HTTPX.is_closed:
        _HTTPX = httpx.AsyncClient(timeout=_FORWARD_TIMEOUT)
    return _HTTPX


def _maybe_extract_project_id(path: str) -> Optional[UUID]:
    """Return the project UUID from path forms like `projects/<uuid>/...`."""
    m = _PROJECT_RE.match(path.lstrip("/"))
    if not m:
        return None
    try:
        return UUID(m.group("pid"))
    except ValueError:
        return None


# Paths that are project-agnostic AND safe to expose to any authenticated
# user. `projects` lists projects (downstream filters server-side);
# `global/model-routes` returns global config rows. Anything else without
# a `projects/<uuid>/...` prefix is denied for non-admins to close the
# IDOR that let `cp/scenarios/<id>`, `cp/render-variants/<id>` etc reach
# any project's data.
_GLOBAL_SAFE_PATHS: tuple[str, ...] = (
    "projects",
    "global/model-routes",
)


def _is_global_safe(path: str) -> bool:
    p = path.lstrip("/").rstrip("/")
    if _PROJECT_RE.match(p):
        return False
    return any(p == sp or p.startswith(sp + "/") for sp in _GLOBAL_SAFE_PATHS)


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


def _enforce_gateway_access(principal: AdminPrincipal, path: str) -> None:
    """Default-deny gate for the content_pipeline proxy.

    Decision tree:
      1. Admins always pass.
      2. Path matches `projects/<uuid>/...` → check membership on that pid.
      3. Path is in `_GLOBAL_SAFE_PATHS` → allow.
      4. Anything else → 404 (don't leak that the resource exists).

    Without this, endpoints like `/cp/scenarios/<id>` slipped through
    project-scope checks because the previous code only enforced when
    the path *literally* started with `projects/<uuid>/`.
    """
    if principal.role == "admin":
        return
    pid = _maybe_extract_project_id(path)
    if pid is not None:
        _check_project_access(principal, pid)
        return
    if _is_global_safe(path):
        return
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


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
        upstream = await _httpx_client().request(
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
    # Hidden from OpenAPI — `openapi_federation` merges the downstream's
    # actual routes under this prefix. Listing the catch-all alongside
    # would just duplicate the menu with a generic `{path}` stub.
    include_in_schema=False,
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
    _enforce_gateway_access(principal, path)
    return await _proxy(
        request=request,
        base_url=settings.CONTENT_PIPELINE_URL,
        api_key=settings.CONTENT_PIPELINE_API_KEY,
        path=path,
    )


# ---- /api/v1/instagram-scraper/{path} → ig_scraper service ----


@router.api_route(
    "/instagram-scraper/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    # See cp proxy above — federation handles the visible routes.
    include_in_schema=False,
)
async def proxy_instagram_scraper(
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


# ---- /api/v1/ad-scraper/{path} → ad_scraper service ----


@router.api_route(
    "/ad-scraper/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    # See cp proxy above — federation handles the visible routes.
    include_in_schema=False,
)
async def proxy_ad_scraper(
    path: str,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_token),
) -> Response:
    """Proxy to ad_scraper. Global admin only — it has no per-project scope."""
    if principal.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    if not settings.AD_SCRAPER_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ad_scraper service token not configured (AD_SCRAPER_API_KEY)",
        )
    return await _proxy(
        request=request,
        base_url=settings.AD_SCRAPER_URL,
        api_key=settings.AD_SCRAPER_API_KEY,
        path=path,
    )
