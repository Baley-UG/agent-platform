"""Database engine and session management.

Two engines are exposed:
- `engine` — primary, used by writes (jobs, upserts, scheduling).
- `read_engine` — points at the read replica when configured, falls back
  to the primary otherwise. Used by analytical / BI endpoints.

Sessions are explicit: callers (FastAPI deps, worker, scheduler) should
use `session_scope()` so we get clean commit/rollback boundaries.
"""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool
from sqlmodel import Session, create_engine, select

from app.core.config import settings
from app.core.logging import logger


def _make_engine(dsn: str, label: str):
    return create_engine(
        dsn,
        pool_pre_ping=True,
        poolclass=QueuePool,
        pool_size=settings.POSTGRES_POOL_SIZE,
        max_overflow=settings.POSTGRES_MAX_OVERFLOW,
        pool_timeout=30,
        pool_recycle=1800,
        echo=False,
    )


engine = _make_engine(settings.postgres_dsn, "primary")
read_engine = (
    _make_engine(settings.POSTGRES_READ_REPLICA_DSN, "replica")
    if settings.POSTGRES_READ_REPLICA_DSN
    else engine
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session_scope() -> Iterator[Session]:
    """Read-only session pointing at the replica when configured."""
    session = Session(read_engine)
    try:
        yield session
    finally:
        session.close()


def health_check() -> bool:
    """Return True if the primary DB is reachable."""
    try:
        with session_scope() as session:
            session.exec(select(1)).first()
        return True
    except SQLAlchemyError as exc:
        logger.warning("db_health_check_failed", error=str(exc))
        return False
