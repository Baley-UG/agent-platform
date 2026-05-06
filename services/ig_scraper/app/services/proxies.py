"""Service-layer logic for ig_proxies."""

import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.proxy import Proxy
from app.schemas.proxies import ProxyCreate, ProxyRead, ProxyTestResponse, ProxyUpdate
from app.services.crypto import encrypt_optional

VALID_PROTOCOLS = {"http", "https", "socks5"}
VALID_PROXY_STATUSES = {"active", "cooldown", "dead"}

# A small JSON-returning IP echo service. Picked because:
# - it returns the public IP (useful sanity check for residential rotations);
# - it's cheap and has no auth;
# - if it ever goes down the test gracefully degrades to "ok=False".
# Configurable via env in a later milestone if needed.
_TEST_TARGET = "https://api.ipify.org?format=json"
_TEST_TIMEOUT_SECONDS = 8.0


class ProxyNotFoundError(Exception):
    """Raised when a proxy lookup returns nothing."""


class InvalidProxyStateError(Exception):
    """Raised when a value violates the protocol/status whitelists."""


def _to_read(proxy: Proxy) -> ProxyRead:
    return ProxyRead(
        id=proxy.id,
        protocol=proxy.protocol,
        host=proxy.host,
        port=proxy.port,
        username=proxy.username,
        label=proxy.label,
        status=proxy.status,
        has_password=bool(proxy.password_enc),
        last_ok_at=proxy.last_ok_at,
        failure_count=proxy.failure_count,
        cooldown_until=proxy.cooldown_until,
        created_at=proxy.created_at,
        updated_at=proxy.updated_at,
    )


def _validate_protocol(protocol: str) -> None:
    if protocol not in VALID_PROTOCOLS:
        raise InvalidProxyStateError(f"protocol must be one of {sorted(VALID_PROTOCOLS)}")


def _validate_status(status_value: str) -> None:
    if status_value not in VALID_PROXY_STATUSES:
        raise InvalidProxyStateError(f"status must be one of {sorted(VALID_PROXY_STATUSES)}")


def create_proxy(session: Session, payload: ProxyCreate) -> ProxyRead:
    """Insert a proxy row, encrypting the password if provided."""
    _validate_protocol(payload.protocol)
    proxy = Proxy(
        protocol=payload.protocol,
        host=payload.host,
        port=payload.port,
        username=payload.username,
        password_enc=encrypt_optional(payload.password),
        label=payload.label,
    )
    session.add(proxy)
    session.flush()
    logger.info("proxy_created", proxy_id=str(proxy.id), host=proxy.host, label=proxy.label)
    return _to_read(proxy)


def list_proxies(session: Session, status_filter: Optional[str] = None) -> List[ProxyRead]:
    """List proxies, optionally filtered by status."""
    stmt = select(Proxy).order_by(Proxy.created_at.desc())
    if status_filter is not None:
        _validate_status(status_filter)
        stmt = stmt.where(Proxy.status == status_filter)
    return [_to_read(p) for p in session.exec(stmt).all()]


def get_proxy(session: Session, proxy_id: uuid.UUID) -> ProxyRead:
    proxy = session.get(Proxy, proxy_id)
    if proxy is None:
        raise ProxyNotFoundError(str(proxy_id))
    return _to_read(proxy)


def _get_proxy_or_raise(session: Session, proxy_id: uuid.UUID) -> Proxy:
    proxy = session.get(Proxy, proxy_id)
    if proxy is None:
        raise ProxyNotFoundError(str(proxy_id))
    return proxy


def update_proxy(session: Session, proxy_id: uuid.UUID, payload: ProxyUpdate) -> ProxyRead:
    proxy = _get_proxy_or_raise(session, proxy_id)
    if payload.protocol is not None:
        _validate_protocol(payload.protocol)
        proxy.protocol = payload.protocol
    if payload.host is not None:
        proxy.host = payload.host
    if payload.port is not None:
        proxy.port = payload.port
    if payload.username is not None:
        proxy.username = payload.username
    if payload.password is not None:
        proxy.password_enc = encrypt_optional(payload.password)
    if payload.label is not None:
        proxy.label = payload.label
    if payload.status is not None:
        _validate_status(payload.status)
        proxy.status = payload.status
    proxy.updated_at = datetime.now(timezone.utc)
    session.add(proxy)
    session.flush()
    logger.info("proxy_updated", proxy_id=str(proxy.id))
    return _to_read(proxy)


def _build_proxy_url_for_test(proxy: Proxy) -> str:
    """Render the proxy as a URL httpx understands, with creds inline if present."""
    from app.services.crypto import decrypt_optional

    creds = ""
    if proxy.username:
        password = decrypt_optional(proxy.password_enc) or ""
        creds = f"{proxy.username}:{password}@"
    return f"{proxy.protocol}://{creds}{proxy.host}:{proxy.port}"


def test_proxy(session: Session, proxy_id: uuid.UUID) -> ProxyTestResponse:
    """Issue a single GET through the proxy and persist the outcome.

    Updates `last_ok_at` / `failure_count` / `status` on the row so the
    pool can later make decisions without re-running the test.
    """
    proxy = _get_proxy_or_raise(session, proxy_id)
    proxy_url = _build_proxy_url_for_test(proxy)

    started = time.perf_counter()
    public_ip: Optional[str] = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    ok = False

    try:
        with httpx.Client(
            proxy=proxy_url,
            timeout=_TEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            transport=httpx.HTTPTransport(retries=0),
        ) as client:
            response = client.get(_TEST_TARGET)
            status_code = response.status_code
            ok = 200 <= response.status_code < 300
            if ok:
                try:
                    public_ip = response.json().get("ip")
                except Exception:  # noqa: BLE001
                    public_ip = None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = int((time.perf_counter() - started) * 1000)

    if ok:
        proxy.last_ok_at = datetime.now(timezone.utc)
        proxy.failure_count = 0
        if proxy.status == "dead":
            proxy.status = "active"
    else:
        proxy.failure_count = (proxy.failure_count or 0) + 1
    proxy.updated_at = datetime.now(timezone.utc)
    session.add(proxy)
    session.flush()

    logger.info(
        "proxy_tested",
        proxy_id=str(proxy.id),
        ok=ok,
        latency_ms=latency_ms,
        status_code=status_code,
        error=error,
    )

    return ProxyTestResponse(
        id=proxy.id,
        ok=ok,
        latency_ms=latency_ms,
        status_code=status_code,
        public_ip=public_ip,
        error=error,
    )
