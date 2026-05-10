"""Bootstrap the first admin user from env on startup.

If `CP_BOOTSTRAP_ADMIN_EMAIL` + `CP_BOOTSTRAP_ADMIN_PASSWORD` are set
AND the users table is empty, create the admin row and log it. Fails
softly: a missing env or an already-populated table is a no-op.

Called from `app.main.lifespan` so dev runs `docker compose up` and
gets a working login without manual seed scripts.
"""

from __future__ import annotations

from sqlmodel import select

from app.core.config import settings
from app.core.logging import logger
from app.models.users import User
from app.services import users as users_svc
from app.services.database import session_scope


def ensure_admin() -> None:
    email = (settings.CP_BOOTSTRAP_ADMIN_EMAIL or "").strip()
    password = settings.CP_BOOTSTRAP_ADMIN_PASSWORD or ""
    if not email or not password:
        logger.info("bootstrap_admin_skipped", reason="no env credentials")
        return

    try:
        with session_scope() as session:
            existing = session.exec(select(User).limit(1)).first()
            if existing is not None:
                logger.info("bootstrap_admin_skipped", reason="users table not empty")
                return
            users_svc.create(
                session,
                email=email,
                password=password,
                name=settings.CP_BOOTSTRAP_ADMIN_NAME or "Admin",
                role="admin",
            )
            logger.info("bootstrap_admin_created", email=email)
    except Exception as exc:  # noqa: BLE001
        # Don't fail startup if bootstrap can't run (e.g. DB still warming).
        logger.warning("bootstrap_admin_failed", error=str(exc))
