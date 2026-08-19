"""YouCloud session token — storage, caching, expiry.

The endpoint authenticates with a single cookie value, `sessionId`, an
ES256 JWT carrying an `exp` claim (~7 days out in practice). **That token
is the only auth mechanism.** We store it, cache it, warn before it dies,
and the operator rotates it:

    PUT /api/v1/credentials/session  {"session_cookie": "<sessionId>"}

Automatic login was considered and dropped. It would have meant storing a
password we'd replay against a login flow we cannot inspect (the endpoint
disables GraphQL introspection), on a platform whose ToS it may breach,
with account lockout as the failure mode — to save a weekly paste. Nothing
here stores a password, so there is none to leak and nothing that can lock
the account out.

No caching. The decrypted token is read from the row on every request,
which costs 0.66 ms — against a rate gate that already holds requests
1500 ms apart. An in-process cache used to live here and was removed: this
service runs two processes, so the API could store a rotated token while
the worker kept using the old one until a job failed. See the note further
down for the full reasoning.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.core.logging import logger
from app.core.metrics import ad_login_failures_total
from app.models.credential import ACTIVE, DISABLED, EXPIRED, LOGIN_FAILED, Credential
from app.services import crypto
from app.services.database import session_scope
from app.services.parsing import jwt_expires_at

DEFAULT_LABEL = "default"


class CredentialError(RuntimeError):
    """No usable YouCloud session token is available."""


# ----------------------------------------------------------------------
# No token cache — deliberately
# ----------------------------------------------------------------------
#
# There used to be a module-level cache of the decrypted token. It was
# removed because a module-level anything is per PROCESS, and this service
# runs two: the API stores a rotated token and primes ITS cache, while the
# worker — the process that actually makes upstream requests — keeps serving
# the old one. Rotating a token therefore did not reach the worker at all
# until a job failed and the rejection path invalidated it. There was even a
# `POST /credentials/session/invalidate-cache` endpoint whose docstring named
# "another replica storing a newer token" as its use case, which is precisely
# the case it could not fix.
#
# What the cache bought, measured: 0.66 ms per read. The rate gate already
# holds requests 1500 ms apart, so it saved 0.04% of one request interval in
# exchange for cross-process staleness. Reading the row every time is the
# cheaper trade.

# ----------------------------------------------------------------------
# Row access
# ----------------------------------------------------------------------


def get_credential(session: Session, label: str = DEFAULT_LABEL) -> Optional[Credential]:
    """Fetch a credential row by label."""
    return session.exec(select(Credential).where(Credential.label == label)).first()


def get_or_create(session: Session, label: str = DEFAULT_LABEL) -> Credential:
    """Fetch the credential row, creating an empty one on first use.

    Lazy creation mirrors content_pipeline's `posting_strategy.get_or_create`:
    the operator's first `GET /credentials` should show them a row to fill
    in, not a 404.
    """
    row = get_credential(session, label)
    if row is not None:
        return row
    row = Credential(label=label, status=EXPIRED)
    session.add(row)
    session.flush()
    logger.info("ad_credential_created", label=label)
    return row


def pick_usable(session: Session) -> Optional[Credential]:
    """Return the healthiest credential that holds a token.

    Prefers `active`, then most recently successful. `disabled` rows are
    never returned — that status is an operator's explicit "do not use".
    """
    rows = session.exec(select(Credential).where(Credential.status != DISABLED)).all()
    candidates = [r for r in rows if r.session_cookie_enc]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            0 if r.status == ACTIVE else 1,
            -(r.last_ok_at.timestamp() if r.last_ok_at else 0),
        )
    )
    return candidates[0]


# ----------------------------------------------------------------------
# Mutations
# ----------------------------------------------------------------------


def set_session_cookie(session: Session, cookie: str, *, label: str = DEFAULT_LABEL) -> Credential:
    """Store a `sessionId` token and derive its expiry from the JWT itself.

    Every process reads the row on its next request, so a rotated token takes
    effect immediately in the worker as well as the API.
    """
    cookie = (cookie or "").strip()
    if not cookie:
        raise ValueError("session cookie must not be empty")

    row = get_or_create(session, label)
    row.session_cookie_enc = crypto.encrypt(cookie)
    row.session_expires_at = jwt_expires_at(cookie)
    row.status = ACTIVE
    row.consecutive_failures = 0
    row.last_error = None
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)

    if row.session_expires_at is None:
        # Not fatal: the token may still be accepted. But we can't warn
        # ahead of its death, so the first rejection becomes the signal.
        logger.warning("ad_session_expiry_unknown", label=label)
    logger.info(
        "ad_session_stored",
        label=label,
        expires_at=row.session_expires_at.isoformat() if row.session_expires_at else None,
    )
    return row


def mark_ok(session: Session, label: str = DEFAULT_LABEL) -> None:
    """Record that a request succeeded on this token."""
    row = get_credential(session, label)
    if row is None:
        return
    now = datetime.now(timezone.utc)
    row.last_ok_at = now
    row.updated_at = now
    row.consecutive_failures = 0
    if row.status != ACTIVE:
        row.status = ACTIVE
    row.last_error = None
    session.add(row)


def mark_rejected(session: Session, reason: str, *, label: str = DEFAULT_LABEL) -> Optional[Credential]:
    """Record that the API rejected the current token, and drop the cache.

    Called from the worker when a job fails with `AuthExpired`. Past
    `AD_LOGIN_MAX_CONSECUTIVE_FAILURES` the row flips to `login_failed`,
    which stops jobs from replaying a token we know is dead and makes the
    dashboard say so.
    """
    row = get_credential(session, label)
    if row is None:
        return None
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error = reason[:500]
    row.updated_at = datetime.now(timezone.utc)
    if row.consecutive_failures >= settings.AD_LOGIN_MAX_CONSECUTIVE_FAILURES:
        row.status = LOGIN_FAILED
        ad_login_failures_total.labels(reason="token_rejected").inc()
        logger.error(
            "ad_credential_locked_out",
            label=label,
            consecutive_failures=row.consecutive_failures,
            note="paste a fresh token via PUT /api/v1/credentials/session",
        )
    else:
        row.status = EXPIRED
        logger.warning("ad_token_rejected", label=label, consecutive_failures=row.consecutive_failures)
    session.add(row)
    return row


def disable(session: Session, *, label: str = DEFAULT_LABEL) -> Optional[Credential]:
    """Take a credential out of service without deleting its token."""
    row = get_credential(session, label)
    if row is None:
        return None
    row.status = DISABLED
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    logger.info("ad_credential_disabled", label=label)
    return row


# ----------------------------------------------------------------------
# Session resolution — what the client calls
# ----------------------------------------------------------------------


def needs_refresh(row: Credential, *, now: Optional[datetime] = None) -> bool:
    """True when the stored token is missing, expired, or expiring soon.

    An unknown expiry is NOT "needs refresh" — see `_CachedToken.is_stale`.
    """
    if not row.session_cookie_enc:
        return True
    if row.session_expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    expires_at = row.session_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - timedelta(seconds=settings.AD_SESSION_REFRESH_MARGIN_SECONDS) <= now


def current_cookie(label: str = DEFAULT_LABEL) -> Optional[str]:
    """Return the usable `sessionId`, or None when there isn't one.

    Reads the row every time — there is no cache, on purpose; see the note
    above. Opens its own DB session because the caller is an async request
    path that holds none.

    Returns None — rather than a stale token — when the row is locked out or
    past its expiry. The client turns that into `AuthExpired`, and the job
    records an actionable reason instead of burning a request on a token the
    server will refuse.
    """
    with session_scope() as session:
        row = get_credential(session, label) or pick_usable(session)
        if row is None or not row.session_cookie_enc:
            return None
        if row.status == LOGIN_FAILED:
            # Locked out: refuse rather than replay a token already rejected.
            return None
        if needs_refresh(row):
            logger.warning(
                "ad_token_expired",
                label=label,
                expires_at=row.session_expires_at.isoformat() if row.session_expires_at else None,
                note="paste a fresh token via PUT /api/v1/credentials/session",
            )
            return None
        cookie = crypto.decrypt(row.session_cookie_enc)

    return cookie


def redacted_view(row: Optional[Credential]) -> dict:
    """Safe representation for the API — never exposes the token itself."""
    if row is None:
        return {
            "label": DEFAULT_LABEL,
            "status": EXPIRED,
            "has_session": False,
            "session_expires_at": None,
            "expires_in_seconds": None,
            "needs_refresh": True,
            "last_ok_at": None,
            "consecutive_failures": 0,
            "last_error": None,
        }

    expires_in: Optional[int] = None
    if row.session_expires_at is not None:
        expires_at = row.session_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_in = int((expires_at - datetime.now(timezone.utc)).total_seconds())

    return {
        "label": row.label,
        "status": row.status,
        "has_session": row.session_cookie_enc is not None,
        "session_expires_at": row.session_expires_at,
        # Negative means already dead — surfaced rather than clamped so the
        # panel can say "expired 3 days ago" instead of just "expired".
        "expires_in_seconds": expires_in,
        "needs_refresh": needs_refresh(row),
        "last_ok_at": row.last_ok_at,
        "consecutive_failures": row.consecutive_failures,
        "last_error": row.last_error,
    }
