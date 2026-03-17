"""Control plane repository tests. Use testcontainers Postgres."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.core.config import get_settings
from src.core.database import _engines, get_control_session
from src.repository.control_plane import (
    ApiKeyRepository,
    TenantDbRoutingRepository,
    TenantRepository,
)


def _ensure_psycopg2(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _run_control_migrations(url: str) -> None:
    env = os.environ.copy()
    env["ALEMBIC_CONTROL_URL"] = url
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.fixture(scope="module")
def control_plane_session() -> Session:
    """Postgres with control plane schema; one container shared across module. Yields a Session."""
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url()
        url = _ensure_psycopg2(url)
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()

        _run_control_migrations(url)

        # Point app at this DB and clear caches
        os.environ["CONTROL_PLANE_DATABASE_URL"] = url
        # Tenant template: same URL but database name is {tenant_id}
        from sqlalchemy.engine import make_url
        u = make_url(url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        get_settings.cache_clear()
        _engines.clear()

        with get_control_session() as session:
            yield session

        _engines.clear()


@pytest.mark.slow
def test_create_tenant(control_plane_session: Session) -> None:
    """Create a tenant; assert tenant_id starts with ten_."""
    repo = TenantRepository(control_plane_session)
    tenant = repo.create(name="Acme", plan="free")
    assert tenant.tenant_id.startswith("ten_")
    assert tenant.name == "Acme"
    assert tenant.plan == "free"
    assert tenant.status == "active"


@pytest.mark.slow
def test_create_api_key_returns_plaintext_once(control_plane_session: Session) -> None:
    """Create a key; assert plaintext starts with lv_, stored hash != plaintext."""
    tenant_repo = TenantRepository(control_plane_session)
    key_repo = ApiKeyRepository(control_plane_session)
    tenant = tenant_repo.create(name="Acme", plan="free")
    api_key, plaintext = key_repo.create(tenant_id=tenant.tenant_id, label="default", role="admin")
    assert plaintext.startswith("lv_")
    assert api_key.key_hash != plaintext
    assert len(api_key.key_hash) == 64  # SHA256 hex


@pytest.mark.slow
def test_get_by_plaintext(control_plane_session: Session) -> None:
    """Create a key, retrieve by plaintext, assert match."""
    tenant_repo = TenantRepository(control_plane_session)
    key_repo = ApiKeyRepository(control_plane_session)
    tenant = tenant_repo.create(name="Acme", plan="free")
    api_key, plaintext = key_repo.create(tenant_id=tenant.tenant_id, label="default", role="admin")
    found = key_repo.get_by_plaintext(plaintext)
    assert found is not None
    assert found.key_id == api_key.key_id
    assert found.tenant_id == tenant.tenant_id


@pytest.mark.slow
def test_revoke_key(control_plane_session: Session) -> None:
    """Create a key, revoke it, assert revoked_at is not None."""
    tenant_repo = TenantRepository(control_plane_session)
    key_repo = ApiKeyRepository(control_plane_session)
    tenant = tenant_repo.create(name="Acme", plan="free")
    api_key, _ = key_repo.create(tenant_id=tenant.tenant_id, label="default", role="admin")
    ok = key_repo.revoke(api_key.key_id, tenant_id=tenant.tenant_id)
    assert ok is True
    # Fetch raw row and assert revoked_at set
    from sqlalchemy import text as sa_text

    with control_plane_session.connection() as conn:
        row = conn.execute(
            sa_text("SELECT revoked_at FROM api_keys WHERE key_id = :key_id"),
            {"key_id": api_key.key_id},
        ).fetchone()
        assert row is not None
        assert row[0] is not None


@pytest.mark.slow
def test_tenant_db_routing(control_plane_session: Session) -> None:
    """Create tenant + routing entry, retrieve by tenant_id."""
    tenant_repo = TenantRepository(control_plane_session)
    routing_repo = TenantDbRoutingRepository(control_plane_session)
    tenant = tenant_repo.create(name="Acme", plan="pro")
    conn_str = f"postgresql+psycopg2://fake/{tenant.tenant_id}"
    routing_repo.create(tenant_id=tenant.tenant_id, connection_string=conn_str, region="local")
    row = routing_repo.get_by_tenant_id(tenant.tenant_id)
    assert row is not None
    assert row.connection_string == conn_str
    assert row.region == "local"
