"""Proxies router — full CRUD + test (M2)."""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.proxies import ProxyCreate, ProxyRead, ProxyTestResponse, ProxyUpdate
from app.services import proxies as proxies_service
from app.services.database import session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=ProxyRead, status_code=status.HTTP_201_CREATED)
def create_proxy(payload: ProxyCreate) -> ProxyRead:
    """Add a proxy. Password (when given) is encrypted at rest."""
    try:
        with session_scope() as session:
            return proxies_service.create_proxy(session, payload)
    except proxies_service.InvalidProxyStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[ProxyRead])
def list_proxies(status_filter: Optional[str] = Query(default=None, alias="status")) -> List[ProxyRead]:
    """List proxies, optionally filtered by status."""
    try:
        with session_scope() as session:
            return proxies_service.list_proxies(session, status_filter=status_filter)
    except proxies_service.InvalidProxyStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{proxy_id}", response_model=ProxyRead)
def get_proxy(proxy_id: uuid.UUID) -> ProxyRead:
    """Fetch a single proxy."""
    try:
        with session_scope() as session:
            return proxies_service.get_proxy(session, proxy_id)
    except proxies_service.ProxyNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="proxy not found")


@router.patch("/{proxy_id}", response_model=ProxyRead)
def update_proxy(proxy_id: uuid.UUID, payload: ProxyUpdate) -> ProxyRead:
    """Patch a subset of mutable fields."""
    try:
        with session_scope() as session:
            return proxies_service.update_proxy(session, proxy_id, payload)
    except proxies_service.ProxyNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="proxy not found")
    except proxies_service.InvalidProxyStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{proxy_id}/test", response_model=ProxyTestResponse)
def test_proxy(proxy_id: uuid.UUID) -> ProxyTestResponse:
    """Issue one GET through the proxy and persist the outcome."""
    try:
        with session_scope() as session:
            return proxies_service.test_proxy(session, proxy_id)
    except proxies_service.ProxyNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="proxy not found")
