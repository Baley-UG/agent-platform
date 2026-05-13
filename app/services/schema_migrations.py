"""Idempotent ALTER-TABLE migrations for the main app.

Main app uses `SQLModel.metadata.create_all(engine)` which adds NEW
tables but never alters existing ones. When we extend an existing table
(e.g. adding `role` to `user` for CP-M9), we need raw `ALTER TABLE ...
ADD COLUMN IF NOT EXISTS` statements that run after `create_all`.

Postgres-specific syntax (`IF NOT EXISTS` on column ADD is supported on
9.6+). On SQLite (dev fallback) the statements would fail, so we skip
when the dialect isn't PostgreSQL.
"""

from __future__ import annotations

from typing import List, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.logging import logger


# (table_name, alter_sql_idempotent)
_ALTERS: List[Tuple[str, str]] = [
    # CP-M9 admin-panel additions on the user table.
    ("user", 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS name TEXT'),
    ("user", "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'member'"),
    ("user", "ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'"),
    ("user", 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP'),
    # Real FK on project_membership.project_id → public.projects.id with
    # CASCADE delete. The model now declares the FK, but create_all won't
    # retro-add a constraint to an existing table. Drop-and-recreate
    # makes it idempotent: drop a constraint by that name if present,
    # then add it. Wrapped in a DO block to swallow "constraint does not
    # exist" on the first run.
    (
        "project_membership",
        """
        DO $$
        BEGIN
            ALTER TABLE project_membership
                DROP CONSTRAINT IF EXISTS project_membership_project_id_fkey;
            -- Only add the FK when public.projects exists yet (content_pipeline
            -- migrations may not have run on a brand-new dev box).
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='public' AND table_name='projects'
            ) THEN
                ALTER TABLE project_membership
                    ADD CONSTRAINT project_membership_project_id_fkey
                    FOREIGN KEY (project_id) REFERENCES public.projects(id)
                    ON DELETE CASCADE;
            END IF;
        END $$
        """,
    ),
]


def apply(engine: Engine) -> None:
    """Run idempotent schema migrations. Safe to call on every startup."""
    if engine.dialect.name != "postgresql":
        logger.info("schema_migrations_skipped_dialect", dialect=engine.dialect.name)
        return

    with engine.begin() as conn:
        for table, sql in _ALTERS:
            try:
                conn.execute(text(sql))
            except Exception as exc:  # noqa: BLE001
                # Idempotent statements should never raise on Postgres 9.6+,
                # but log defensively in case the syntax isn't supported.
                logger.warning("schema_migration_failed", table=table, sql=sql, error=str(exc))
    logger.info("schema_migrations_applied", count=len(_ALTERS))
