"""Request/response shapes for the session-token endpoints.

The read shape NEVER carries the token — only whether one exists and how
long it has left. Same rule ig_scraper applies to `ig_accounts.session_blob`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CredentialRead(BaseModel):
    """Safe view of the stored YouCloud session token."""

    label: str
    status: str
    has_session: bool
    session_expires_at: Optional[datetime] = None
    # Negative when the token is already dead — reported rather than clamped
    # so a panel can say "expired 3 days ago", not just "expired".
    expires_in_seconds: Optional[int] = None
    needs_refresh: bool
    last_ok_at: Optional[datetime] = None
    consecutive_failures: int
    last_error: Optional[str] = None


class SessionUpdate(BaseModel):
    """Store a `sessionId` token captured from a logged-in browser.

    This is the auth mechanism, not a workaround: copy the `sessionId`
    cookie value from a logged-in appgrowing session. The value is a JWT, so
    its expiry is derived automatically — no need to state it.
    """

    session_cookie: str = Field(min_length=16, description="The raw `sessionId` cookie value (a JWT).")
