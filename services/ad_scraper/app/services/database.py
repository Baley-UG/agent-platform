"""Database engine and session management.

Two engines are exposed:
- `engine` — primary, used by writes (jobs, upserts, credentials).
- `read_engine` — points at the read replica when configured, falls back
  to the primary otherwise. Used by the read/analytical endpoints.

Sessions are explicit: callers (FastAPI deps, worker) should use
`session_scope()` so we get clean commit/rollback boundaries.
"""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool
from sqlmodel import Session, create_engine

from app.core.config import settings


def _make_engine(dsn: str):
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


engine = _make_engine(settings.postgres_dsn)
read_engine = _make_engine(settings.POSTGRES_READ_REPLICA_DSN) if settings.POSTGRES_READ_REPLICA_DSN else engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    `expire_on_commit=False` keeps ORM attributes valid after `commit()`
    so a row claimed inside one scope can be read across subsequent
    scopes without triggering DetachedInstanceError. Stale-read tradeoff:
    after commit, attribute values reflect the in-memory state, not a
    re-read from the DB. Caller must refresh explicitly when needed.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        raise
    finally:
        session.close()
