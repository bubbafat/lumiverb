"""Database connection management for control plane and per-tenant databases."""

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.engine import create_engine, make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from src.core.config import get_settings

# Module-level cache: URL -> Engine. Avoid creating a new engine per request.
_engines: dict[str, Engine] = {}


def _engine_kwargs() -> dict:
    """Return create_engine kwargs — uses NullPool when SQLALCHEMY_NULLPOOL is set (e.g. tests)."""
    if os.environ.get("SQLALCHEMY_NULLPOOL"):
        return {"poolclass": NullPool}
    return {"pool_pre_ping": True}


def get_control_engine() -> Engine:
    """Return a cached SQLAlchemy engine for the control plane DB."""
    settings = get_settings()
    url = settings.control_plane_database_url
    if url not in _engines:
        _engines[url] = create_engine(url, **_engine_kwargs())
    return _engines[url]


def get_tenant_engine(tenant_id: str) -> Engine:
    """Return a cached engine for a specific tenant DB."""
    settings = get_settings()
    url = settings.tenant_database_url_template.format(tenant_id=tenant_id)
    return get_engine_for_url(url)


def get_engine_for_url(url: str) -> Engine:
    """Return a cached engine for the given database URL (e.g. from tenant_db_routing)."""
    if url not in _engines:
        _engines[url] = create_engine(url, **_engine_kwargs())
    return _engines[url]


@contextmanager
def get_control_session() -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session for the control plane."""
    engine = get_control_engine()
    with Session(engine) as session:
        yield session


@contextmanager
def get_tenant_session(tenant_id: str) -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session for a tenant DB."""
    engine = get_tenant_engine(tenant_id)
    with Session(engine) as session:
        yield session


# Tenant ID from our code is always "ten_" + ULID (alphanumeric + underscore). Allow only safe identifiers.
_SAFE_TENANT_ID = re.compile(r"^[a-zA-Z0-9_]+$")


def provision_tenant_database(tenant_id: str) -> None:
    """
    Create the tenant database if it doesn't exist, enable pgvector, and run
    alembic -c alembic-tenant.ini upgrade heads against it.
    Raises on failure.
    """
    if not _SAFE_TENANT_ID.match(tenant_id):
        raise ValueError(f"Invalid tenant_id for database creation: {tenant_id!r}")

    settings = get_settings()
    tenant_url = settings.tenant_database_url_template.format(tenant_id=tenant_id)
    url_obj = make_url(tenant_url)

    # Connect to default 'postgres' database to create the tenant database
    postgres_url = url_obj.set(database="control_plane").render_as_string(hide_password=False)

    engine = create_engine(postgres_url, isolation_level="AUTOCOMMIT")

    # Quote identifier for CREATE DATABASE (preserve case, prevent injection)
    safe_name = tenant_id.replace('"', '""')
    db_name_quoted = f'"{safe_name}"'

    with engine.connect() as conn:
        # Create database if not exists (PostgreSQL has no IF NOT EXISTS for CREATE DATABASE before PG 15;
        # we use a simple CREATE and catch duplicate. PG 15+ has CREATE DATABASE ... IF NOT EXISTS.)
        try:
            conn.execute(text(f"CREATE DATABASE {db_name_quoted}"))
        except Exception as e:
            err = str(e).lower()
            if "already exists" not in err and "duplicate" not in err:
                raise
    engine.dispose()

    # Enable pgvector in the new database
    tenant_engine = create_engine(tenant_url)
    with tenant_engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    tenant_engine.dispose()

    # Run tenant migrations via subprocess (same pattern as migration test)
    env = os.environ.copy()
    env["ALEMBIC_TENANT_URL"] = tenant_url
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", "heads"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade heads failed for tenant {tenant_id}: {result.stderr or result.stdout}"
        )


def deprovision_tenant_database(tenant_id: str) -> None:
    """Drop the physical tenant database. No-op if it doesn't exist. Raises on unexpected errors."""
    if not _SAFE_TENANT_ID.match(tenant_id):
        raise ValueError(f"Invalid tenant_id for database drop: {tenant_id!r}")

    settings = get_settings()
    tenant_url = settings.tenant_database_url_template.format(tenant_id=tenant_id)
    url_obj = make_url(tenant_url)
    postgres_url = url_obj.set(database="control_plane").render_as_string(hide_password=False)

    engine = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    safe_name = tenant_id.replace('"', '""')
    db_name_quoted = f'"{safe_name}"'
    with engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {db_name_quoted}"))
    engine.dispose()
