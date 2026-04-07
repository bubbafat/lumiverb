"""Alembic env for tenant migrations. Uses SQLModel metadata, sync psycopg2."""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine
from sqlalchemy import pool
from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

try:
    from src.server.models.tenant import SQLModel  # type: ignore[attr-defined]
    target_metadata = SQLModel.metadata
except ImportError:
    from sqlmodel import SQLModel
    target_metadata = SQLModel.metadata


def get_url() -> str:
    """Database URL: env ALEMBIC_TENANT_URL overrides alembic.ini."""
    return os.environ.get("ALEMBIC_TENANT_URL") or config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
