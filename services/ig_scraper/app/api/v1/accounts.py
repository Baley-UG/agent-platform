"""Accounts router — full CRUD + login (M2)."""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.deps import require_api_key
from app.schemas.accounts import (
    AccountCreate,
    AccountLoginRequest,
    AccountLoginResponse,
    AccountRead,
    AccountUpdate,
    SessionImportRequest,
)
from app.services import accounts as accounts_service
from app.services.database import session_scope

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate) -> AccountRead:
    """Register a new scraping account. Server encrypts the password."""
    try:
        with session_scope() as session:
            return accounts_service.create_account(session, payload)
    except accounts_service.AccountConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    except accounts_service.InvalidAccountStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[AccountRead])
def list_accounts(status_filter: Optional[str] = Query(default=None, alias="status")) -> List[AccountRead]:
    """List accounts, optionally filtered by status."""
    try:
        with session_scope() as session:
            return accounts_service.list_accounts(session, status=status_filter)
    except accounts_service.InvalidAccountStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{account_id}", response_model=AccountRead)
def get_account(account_id: uuid.UUID) -> AccountRead:
    """Fetch a single account."""
    try:
        with session_scope() as session:
            return accounts_service.get_account(session, account_id)
    except accounts_service.AccountNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(account_id: uuid.UUID, payload: AccountUpdate) -> AccountRead:
    """Patch a subset of mutable fields."""
    try:
        with session_scope() as session:
            return accounts_service.update_account(session, account_id, payload)
    except accounts_service.AccountNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")
    except accounts_service.InvalidAccountStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{account_id}/disable", response_model=AccountRead)
def disable_account(account_id: uuid.UUID) -> AccountRead:
    """Mark an account `disabled`."""
    try:
        with session_scope() as session:
            return accounts_service.disable_account(session, account_id)
    except accounts_service.AccountNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")


@router.post("/{account_id}/login", response_model=AccountLoginResponse)
async def login_account(
    account_id: uuid.UUID, payload: Optional[AccountLoginRequest] = None
) -> AccountLoginResponse:
    """Run the instagrapi login flow and persist the result.

    Returns the new status. `challenge_required` means the operator
    needs to feed a 2FA / SMS / email code via this endpoint again
    using the `verification_code` field.
    """
    code = payload.verification_code if payload else None
    try:
        with session_scope() as session:
            updated, outcome = await accounts_service.run_login(session, account_id, code)
    except accounts_service.AccountNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")

    return AccountLoginResponse(
        id=updated.id,
        status=updated.status,
        last_login_at=updated.last_login_at,
        has_session=updated.has_session,
        detail=outcome.detail or ("ok" if updated.status == "active" else "login failed"),
        ig_message=outcome.ig_message,
        error_type=outcome.error_type,
        exception_name=outcome.exception_name,
    )


@router.post("/{account_id}/import-session", response_model=AccountLoginResponse)
async def import_session_endpoint(
    account_id: uuid.UUID, payload: SessionImportRequest
) -> AccountLoginResponse:
    """Bypass username/password login by importing browser session cookies.

    Useful when IG flags scraper logins from a fresh device fingerprint
    even though the account is healthy. Steps:

    1. Manually login to instagram.com in your browser.
    2. F12 → Application → Cookies → https://www.instagram.com.
    3. Copy the `sessionid` value.
    4. POST it here. We feed it to instagrapi, run a probe, save the
       resulting session if the probe passes.
    """
    if not payload.sessionid and not payload.cookies:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide either `sessionid` or `cookies`.",
        )
    try:
        with session_scope() as session:
            updated, outcome = await accounts_service.import_session(
                session,
                account_id,
                sessionid=payload.sessionid,
                cookies=payload.cookies,
            )
    except accounts_service.AccountNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")

    return AccountLoginResponse(
        id=updated.id,
        status=updated.status,
        last_login_at=updated.last_login_at,
        has_session=updated.has_session,
        detail=outcome.detail or ("ok" if updated.status == "active" else "import failed"),
        ig_message=outcome.ig_message,
        error_type=outcome.error_type,
        exception_name=outcome.exception_name,
    )
