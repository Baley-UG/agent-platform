"""Social account CRUD service.

Credentials are encrypted at rest via Fernet (`app.core.security`). The
read schema deliberately omits them — they only travel back out for the
publisher worker via a dedicated decrypt helper.
"""

from __future__ import annotations

import json
import uuid
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core import security
from app.models.social_accounts import SocialAccount
from app.schemas.social_accounts import SocialAccountCreate, SocialAccountRead, SocialAccountUpdate


def _to_read(row: SocialAccount) -> SocialAccountRead:
    return SocialAccountRead(
        id=row.id,
        project_id=row.project_id,
        provider=row.provider,
        handle=row.handle,
        external_account_id=row.external_account_id,
        status=row.status,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        has_credentials=row.credentials_encrypted is not None,
    )


def create(session: Session, project_id: uuid.UUID, payload: SocialAccountCreate) -> SocialAccountRead:
    encrypted = security.encrypt_optional(json.dumps(payload.credentials)) if payload.credentials else None
    account = SocialAccount(
        project_id=project_id,
        provider=payload.provider,
        handle=payload.handle,
        external_account_id=payload.external_account_id,
        credentials_encrypted=encrypted,
        status="active" if encrypted else "pending_oauth",
    )
    session.add(account)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"social_account already exists: {payload.provider}/{payload.handle}",
        ) from exc
    session.refresh(account)
    return _to_read(account)


def list_(session: Session, project_id: uuid.UUID) -> List[SocialAccountRead]:
    stmt = select(SocialAccount).where(SocialAccount.project_id == project_id).order_by(SocialAccount.created_at.desc())
    return [_to_read(row) for row in session.exec(stmt).all()]


def _get_row(session: Session, project_id: uuid.UUID, account_id: uuid.UUID) -> SocialAccount:
    row = session.get(SocialAccount, account_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="social_account not found")
    return row


def get(session: Session, project_id: uuid.UUID, account_id: uuid.UUID) -> SocialAccountRead:
    return _to_read(_get_row(session, project_id, account_id))


def update(
    session: Session, project_id: uuid.UUID, account_id: uuid.UUID, payload: SocialAccountUpdate
) -> SocialAccountRead:
    row = _get_row(session, project_id, account_id)
    data = payload.model_dump(exclude_unset=True)
    if "credentials" in data:
        creds = data.pop("credentials")
        row.credentials_encrypted = security.encrypt(json.dumps(creds)) if creds else None
        if creds and row.status == "pending_oauth":
            row.status = "active"
    for key, value in data.items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    session.refresh(row)
    return _to_read(row)


def delete(session: Session, project_id: uuid.UUID, account_id: uuid.UUID) -> None:
    row = _get_row(session, project_id, account_id)
    session.delete(row)
    session.flush()


def get_decrypted_credentials(session: Session, project_id: uuid.UUID, account_id: uuid.UUID) -> Optional[dict]:
    """Return the decrypted credential blob. Used internally by the publisher worker."""
    row = _get_row(session, project_id, account_id)
    if row.credentials_encrypted is None:
        return None
    raw = security.decrypt(row.credentials_encrypted)
    return json.loads(raw)
