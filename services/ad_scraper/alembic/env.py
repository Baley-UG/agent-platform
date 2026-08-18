"""Alembic environment.

DSN is taken from the service settings (which read the POSTGRES_* env vars
shared with agent-platform). Importing `app.models` registers every table
on `SQLModel.metadata` so autogenerate works.

**`version_table` is NOT the default.** ad_scraper's tables live in the
shared `public` schema behind the `ad_*` prefix — the same decision
ig_scraper made with `ig_*` — which means both services would otherwise
share one `public.alembic_version` row and each would read the other's
revision id as an unknown revision. content_pipeline sidesteps this by
owning a whole Postgres schema; we keep the shared schema and give this
service its own bookkeeping table instead.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# Side-effect import: registers every table on SQLModel.metadata.
import app.models  # noqa: F401
from app.core.config import settings

VERSION_TABLE = "ad_alembic_version"

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

target_metadata = SQLModel.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate blind to the other services' tables.

    Without this, `alembic revision --autogenerate` would see ig_scraper's
    `ig_*` tables in the same schema, find no matching models, and propose
    dropping all of them.
    """
    if type_ == "table" and not name.startswith("ad_"):
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emits SQL to stdout)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=VERSION_TABLE,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table=VERSION_TABLE,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
