"""Alembic environment for content_pipeline.

DSN comes from service settings (shared POSTGRES_* env). Importing
`app.models` registers every table on SQLModel.metadata so autogenerate
works. Schema `content_pipeline` is included in `version_table_schema`
so the alembic_version table sits next to our tables, not in `public`.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import app.models  # noqa: F401  (registers tables)
from app.core.config import settings
from app.models._base import SCHEMA_NAME

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

target_metadata = SQLModel.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    """Restrict autogenerate to the content_pipeline schema."""
    if type_ == "table" and getattr(obj, "schema", None) != SCHEMA_NAME:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
        version_table_schema=SCHEMA_NAME,
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
        # Make sure the schema exists before alembic tries to read its
        # version table.
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA_NAME}"')
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_schemas=True,
            version_table_schema=SCHEMA_NAME,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
