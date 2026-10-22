"""Database connection management for control plane and per-tenant databases."""

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import event, text
from sqlalchemy.engine import create_engine, make_url
from sqlalchemy.engine.base import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session

from src.server.config import get_settings
from src.shared.secrets_env import without_secrets

# Module-level cache: URL -> Engine. Avoid creating a new engine per request.
_engines: dict[str, Engine] = {}


# Who these connections are, as PostgreSQL shows them (pg_stat_activity's
# application_name): the API and the scheduler say so as they start, so a
# restore that finds Lumiverb still connected can say which part
# (src/server/backups.py connected_elsewhere).
APPLICATION_NAME = "lumiverb"


def set_application_name(name: str) -> None:
    """Names this process's connections from now on (engines made after)."""
    global APPLICATION_NAME
    APPLICATION_NAME = name


# How long a connection may take to be made, in seconds (libpq's connect_timeout): without one, a database
# host that drops what it's sent takes minutes and one that never answers takes forever, holding up the
# request, or the scheduler's work for that account until a restart. The plug-in runner sets less
# (src/server/plugin_runner.py CONNECT_WITHIN_SEC).
CONNECT_TIMEOUT_SEC: int | None = 10
# A connection made is kept checked (TCP keepalives, libpq's): one whose database host went away is given
# up on within a minute or so (30 s quiet, then 3 probes 10 s apart), not left waiting for the system's
# own, which takes hours.
KEEPALIVES = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}


def set_connect_timeout(seconds: int) -> None:
    """Gives up on a connection not made within seconds, from now on (engines made after)."""
    global CONNECT_TIMEOUT_SEC
    CONNECT_TIMEOUT_SEC = seconds


def _engine_kwargs() -> dict:
    """Return create_engine kwargs — uses NullPool when SQLALCHEMY_NULLPOOL is set (e.g. tests)."""
    if os.environ.get("SQLALCHEMY_NULLPOOL"):
        return {"poolclass": NullPool}
    return {"pool_pre_ping": True}


def _create_engine(url: str) -> Engine:
    """An engine whose sessions run in UTC, whatever the server's default:
    Ubuntu's Postgres takes the machine's timezone (prod's is New York),
    and timestamps should read the same from every install."""
    connect_args: dict = {"application_name": APPLICATION_NAME, **KEEPALIVES}
    if CONNECT_TIMEOUT_SEC is not None:
        connect_args["connect_timeout"] = CONNECT_TIMEOUT_SEC
    engine = create_engine(url, connect_args=connect_args, **_engine_kwargs())

    @event.listens_for(engine, "connect")
    def _utc(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.close()
        dbapi_conn.commit()

    return engine


def get_control_engine() -> Engine:
    """Return a cached SQLAlchemy engine for the control plane DB."""
    settings = get_settings()
    url = settings.control_plane_database_url
    if url not in _engines:
        _engines[url] = _create_engine(url)
    return _engines[url]


def tenant_database_url(tenant_id: str) -> str:
    """Where the account's database is: its routing row, as requests read it
    (src/server/api/middleware.py) and a restore rewrites it. The template
    names only a new account's database (src/server/accounts.py)."""
    from src.server.repository.control_plane import TenantDbRoutingRepository

    with get_control_session() as session:
        routing = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
    if routing is None:
        raise LookupError(f"no database for {tenant_id}")
    return routing.connection_string


def get_tenant_engine(tenant_id: str) -> Engine:
    """Return a cached engine for the account's database (tenant_database_url)."""
    return get_engine_for_url(tenant_database_url(tenant_id))


def get_engine_for_url(url: str) -> Engine:
    """Return a cached engine for the given database URL (e.g. from tenant_db_routing)."""
    if url not in _engines:
        _engines[url] = _create_engine(url)
    return _engines[url]


def reachable(session: Session) -> None:
    """Raises unless the session's database answers."""
    session.execute(text("SELECT 1"))


@contextmanager
def get_control_session() -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session for the control plane."""
    engine = get_control_engine()
    with Session(engine) as session:
        yield session


@contextmanager
def get_tenant_session(tenant_id: str) -> Generator[Session, None, None]:
    """Context manager yielding a SQLModel Session for the account's database
    (tenant_database_url): for work outside a request, which has no
    request.state.connection_string."""
    engine = get_tenant_engine(tenant_id)
    with Session(engine) as session:
        session.info["tenant_id"] = tenant_id  # whose it is (its AI jobs' models are the account's)
        yield session


# Tenant ID from our code is always "ten_" + ULID (alphanumeric + underscore). Allow only safe identifiers.
_SAFE_TENANT_ID = re.compile(r"^[a-zA-Z0-9_]+$")
# An account's id exactly as Lumiverb makes one (repository/control_plane.py: "ten_" and a ULID), the shape a
# device's id is held to too (dev_, tenant migration c6d2e8a4f1b7): where an id from outside (a backup's
# manifest) would name a database.
ACCOUNT_ID = re.compile(r"ten_[0-9A-Z]{26}")


def provision_tenant_database(tenant_id: str) -> None:
    """
    Create the tenant database if it doesn't exist, enable pgvector, and run
    alembic -c alembic-tenant.ini upgrade heads against it.
    Raises on failure.
    """
    if not _SAFE_TENANT_ID.fullmatch(tenant_id):
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
    env = without_secrets(os.environ)  # its database's URL alone of the API's secrets
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
    if not _SAFE_TENANT_ID.fullmatch(tenant_id):
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
