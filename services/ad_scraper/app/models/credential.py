"""ad_credentials — the YouCloud session token.

One row is expected in practice, but the table isn't constrained to a
singleton: a second seat (different plan, different region) is a plausible
future need, and `pick_usable` orders by health to choose between them.

**There is no password column.** The session token is the only auth
mechanism; automatic login was considered and dropped (see
`app/services/credentials.py` for why). Nothing here can leak a password
or get the account locked out.

`session_cookie_enc` is Fernet ciphertext, never plaintext.
`session_expires_at` comes from the `exp` claim of that token — it is a
JWT. We read the claim without verifying the signature: we aren't
authenticating the token, only asking when the server will stop accepting
it.
"""

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow

ACTIVE = "active"
EXPIRED = "expired"
# Kept as `login_failed` rather than renamed: it is the terminal "stop
# replaying this token" state, and the Prometheus counter and dashboards
# already key off the name.
LOGIN_FAILED = "login_failed"
DISABLED = "disabled"

VALID_CREDENTIAL_STATUSES: frozenset[str] = frozenset({ACTIVE, EXPIRED, LOGIN_FAILED, DISABLED})


class Credential(SQLModel, table=True):
    """A YouCloud session token and its health."""

    __tablename__ = "ad_credentials"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    label: str = Field(default="default", max_length=64)

    session_cookie_enc: Optional[bytes] = Field(
        default=None, sa_column=sa.Column("session_cookie_enc", sa.LargeBinary, nullable=True)
    )
    session_expires_at: Optional[datetime] = Field(default=None)

    status: str = Field(default=EXPIRED, index=True)
    last_ok_at: Optional[datetime] = Field(default=None)
    consecutive_failures: int = Field(default=0)
    last_error: Optional[str] = Field(default=None)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
