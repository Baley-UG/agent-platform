"""This file contains the user model for the application.

Single `user` table covers BOTH chatbot users and admin-panel users.
The `role` column ('admin' | 'member' | 'service') gates admin access;
chatbot endpoints don't read it. Admin-only endpoints under
`/api/v1/admin/*` require `role == 'admin'`.
"""

from datetime import datetime
from typing import (
    TYPE_CHECKING,
    List,
    Optional,
)

import bcrypt
from sqlmodel import (
    Field,
    Relationship,
)

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.session import Session


GLOBAL_ROLES = ("admin", "member", "service")
USER_STATUSES = ("active", "disabled")


class User(BaseModel, table=True):
    """Single user model: chatbot users + admin-panel users."""

    id: int = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str

    # CP-M9 admin-panel additions. Defaults keep existing chatbot users
    # behaving as 'member', and the chatbot path doesn't read these.
    name: Optional[str] = Field(default=None)
    role: str = Field(default="member")
    status: str = Field(default="active")
    last_login_at: Optional[datetime] = Field(default=None)

    sessions: List["Session"] = Relationship(back_populates="user")

    def verify_password(self, password: str) -> bool:
        """Verify if the provided password matches the hash."""
        return bcrypt.checkpw(password.encode("utf-8"), self.hashed_password.encode("utf-8"))

    @staticmethod
    def verify_password_against_hash(password: str, hashed: str) -> bool:
        """Stateless variant used by the auth service to mitigate the
        login timing oracle. Verifies a submitted password against an
        arbitrary hash (typically a precomputed dummy) so the
        unknown-email branch burns the same bcrypt cycles as a real
        login. Result is discarded; only side-effect (CPU time) matters.
        """
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt (existing chatbot password semantics)."""
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


# Avoid circular imports
from app.models.session import Session  # noqa: E402
