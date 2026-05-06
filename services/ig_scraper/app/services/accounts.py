"""Service-layer logic for ig_accounts.

Endpoints in app/api/v1/accounts.py are thin wrappers — all the
encryption / login / state-machine logic lives here and is unit-testable
without spinning up FastAPI.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.logging import logger
from app.models.account import Account
from app.models.proxy import Proxy
from app.schemas.accounts import AccountCreate, AccountRead, AccountUpdate
from app.services.crypto import encrypt
from app.services.instagrapi_client import login_account

# Whitelisted enum-ish values; anything else is rejected at the service
# boundary so we don't end up with garbage in the DB.
VALID_ROLES = {"scraper", "canary"}
VALID_STATUSES = {"active", "cooldown", "challenge_required", "banned", "disabled"}
VALID_QUOTA_TIERS = {"fresh", "mid", "warm"}


class AccountConflictError(Exception):
    """Raised when uniqueness constraints (username) reject the insert."""


class AccountNotFoundError(Exception):
    """Raised when a lookup by id returns nothing."""


class InvalidAccountStateError(Exception):
    """Raised when a value violates one of the enum-ish whitelists."""


def _to_read(account: Account) -> AccountRead:
    """Strip secrets while serialising — never return password_enc/session_blob."""
    return AccountRead(
        id=account.id,
        username=account.username,
        status=account.status,
        role=account.role,
        proxy_id=account.proxy_id,
        timezone=account.timezone,
        active_hours_start=account.active_hours_start,
        active_hours_end=account.active_hours_end,
        weekday_pattern=account.weekday_pattern,
        quota_tier=account.quota_tier,
        cooldown_until=account.cooldown_until,
        last_used_at=account.last_used_at,
        last_login_at=account.last_login_at,
        failure_count=account.failure_count,
        notes=account.notes,
        has_session=bool(account.session_blob),
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise InvalidAccountStateError(f"role must be one of {sorted(VALID_ROLES)}")


def _validate_status(status: str) -> None:
    if status not in VALID_STATUSES:
        raise InvalidAccountStateError(f"status must be one of {sorted(VALID_STATUSES)}")


def _validate_quota_tier(tier: str) -> None:
    if tier not in VALID_QUOTA_TIERS:
        raise InvalidAccountStateError(f"quota_tier must be one of {sorted(VALID_QUOTA_TIERS)}")


def _ensure_proxy_exists(session: Session, proxy_id: Optional[uuid.UUID]) -> None:
    """Cheap pre-flight so we return 400 instead of leaking an FK error."""
    if proxy_id is None:
        return
    proxy = session.get(Proxy, proxy_id)
    if proxy is None:
        raise InvalidAccountStateError(f"proxy_id {proxy_id} does not exist")


def create_account(session: Session, payload: AccountCreate) -> AccountRead:
    """Insert a new account with the password encrypted at rest."""
    _validate_role(payload.role)
    _ensure_proxy_exists(session, payload.proxy_id)

    account = Account(
        username=payload.username,
        password_enc=encrypt(payload.password),
        proxy_id=payload.proxy_id,
        role=payload.role,
        timezone=payload.timezone,
        active_hours_start=payload.active_hours_start,
        active_hours_end=payload.active_hours_end,
        weekday_pattern=payload.weekday_pattern,
        notes=payload.notes,
        # Status starts as `disabled` — a successful login flips it to `active`.
        status="disabled",
    )
    session.add(account)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise AccountConflictError(f"username '{payload.username}' already exists") from exc

    logger.info("account_created", account_id=str(account.id), username=payload.username)
    return _to_read(account)


def list_accounts(session: Session, status: Optional[str] = None) -> List[AccountRead]:
    """List accounts, optionally filtered by status."""
    stmt = select(Account).order_by(Account.created_at.desc())
    if status is not None:
        _validate_status(status)
        stmt = stmt.where(Account.status == status)
    return [_to_read(a) for a in session.exec(stmt).all()]


def get_account(session: Session, account_id: uuid.UUID) -> AccountRead:
    """Fetch a single account by id."""
    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFoundError(str(account_id))
    return _to_read(account)


def _get_account_or_raise(session: Session, account_id: uuid.UUID) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise AccountNotFoundError(str(account_id))
    return account


def update_account(session: Session, account_id: uuid.UUID, payload: AccountUpdate) -> AccountRead:
    """Apply a partial update and return the new state."""
    account = _get_account_or_raise(session, account_id)

    if payload.role is not None:
        _validate_role(payload.role)
        account.role = payload.role
    if payload.status is not None:
        _validate_status(payload.status)
        account.status = payload.status
    if payload.quota_tier is not None:
        _validate_quota_tier(payload.quota_tier)
        account.quota_tier = payload.quota_tier
    if payload.proxy_id is not None:
        _ensure_proxy_exists(session, payload.proxy_id)
        account.proxy_id = payload.proxy_id
    if payload.password is not None:
        account.password_enc = encrypt(payload.password)
    if payload.timezone is not None:
        account.timezone = payload.timezone
    if payload.active_hours_start is not None:
        account.active_hours_start = payload.active_hours_start
    if payload.active_hours_end is not None:
        account.active_hours_end = payload.active_hours_end
    if payload.weekday_pattern is not None:
        account.weekday_pattern = payload.weekday_pattern
    if payload.notes is not None:
        account.notes = payload.notes

    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    session.flush()
    logger.info("account_updated", account_id=str(account.id))
    return _to_read(account)


def disable_account(session: Session, account_id: uuid.UUID) -> AccountRead:
    """Set status='disabled' regardless of current state."""
    account = _get_account_or_raise(session, account_id)
    account.status = "disabled"
    account.updated_at = datetime.now(timezone.utc)
    session.add(account)
    session.flush()
    logger.info("account_disabled", account_id=str(account.id), username=account.username)
    return _to_read(account)


async def run_login(
    session: Session,
    account_id: uuid.UUID,
    verification_code: Optional[str],
) -> AccountRead:
    """Run the instagrapi login flow and persist the result.

    Updates `status`, `session_blob`, `last_login_at`, `failure_count`.
    Failure count is incremented on terminal failures and reset to 0 on
    success.
    """
    account = _get_account_or_raise(session, account_id)
    proxy: Optional[Proxy] = None
    if account.proxy_id is not None:
        proxy = session.get(Proxy, account.proxy_id)

    outcome = await login_account(account, proxy, verification_code=verification_code)

    account.session_blob = outcome.session_blob
    account.status = outcome.status
    account.last_login_at = datetime.now(timezone.utc)
    account.updated_at = account.last_login_at
    if outcome.status == "active":
        account.failure_count = 0
    else:
        account.failure_count = (account.failure_count or 0) + 1

    session.add(account)
    session.flush()

    logger.info(
        "account_login_completed",
        account_id=str(account.id),
        status=outcome.status,
        detail=outcome.detail,
    )
    return _to_read(account)
