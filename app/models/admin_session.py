"""Admin-panel refresh-token store.

Distinct from the chatbot `Session` model (which holds chatbot conversation
sessions). This table holds only refresh-token rows for the admin-panel
JWT flow: SHA-256 hash of the raw refresh token, an expiry, optional
device metadata, and a `revoked_at` for logout / "sign out other devices".
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class AdminSession(SQLModel, table=True):
    """One row per active refresh token."""

    __tablename__ = "admin_session"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    token_hash: str = Field(index=True)
    expires_at: datetime
    last_used_at: Optional[datetime] = None
    user_agent: Optional[str] = None
    ip: Optional[str] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
