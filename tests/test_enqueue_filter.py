"""Slow tests for enqueue_jobs_for_filter with AssetFilterSpec (path, asset_id, force, missing_proxy)."""

import os
import secrets
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer
from ulid import ULID

from src.core.config import get_settings
from src.core.database import _engines, get_engine_for_url
from src.models.filter import AssetFilterSpec
from src.models.tenant import Asset
from src.repository.tenant import AssetRepository, LibraryRepository, WorkerJobRepository
from src.workers.enqueue import enqueue_jobs_for_filter
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def filter_test_env() -> tuple[str, str, str]:
    """Same as enqueue_test_env: control + tenant Postgres, tenant created and routed. Yields (tenant_url, api_key, tenant_id)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()

        _run_control_migrations(control_url)
        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            from fastapi.testclient import TestClient
            from src.api.main import app

            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "FilterEnqueueTenant_" + secrets.token_urlsafe(6), "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            yield tenant_url, api_key, tenant_id

        _engines.clear()


@pytest.mark.slow
def test_enqueue_by_path_prefix(filter_test_env: tuple[str, str, str]) -> None:
    """Create assets in A/ and B/, enqueue with path_prefix='B', assert only B assets enqueued."""
    tenant_url, _api_key, _tenant_id = filter_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="PathPrefix_" + secrets.token_urlsafe(6),
            root_path="/path-prefix",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        assets = []
        for prefix in ("A", "B"):
            for i in range(3):
                assets.append({
                    "asset_id": "ast_" + str(ULID()),
                    "library_id": library_id,
                    "rel_path": f"{prefix}/file_{i}.jpg",
                    "file_size": 1000,
                    "media_type": "image/jpeg",
                    "status": "pending",
                    "availability": "online",
                    "created_at": now,
                    "updated_at": now,
                })
        session.execute(insert(Asset), assets)
        session.commit()

        spec = AssetFilterSpec(library_id=library_id, path_prefix="B")
        n = enqueue_jobs_for_filter(session, spec, "proxy", force=False)
        assert n == 3

        rows = session.execute(
            text(
                "SELECT a.rel_path FROM assets a JOIN worker_jobs w ON w.asset_id = a.asset_id "
                "WHERE w.job_type = 'proxy' AND a.library_id = :lib"
            ),
            {"lib": library_id},
        ).fetchall()
        paths = {r[0] for r in rows}
        assert paths == {"B/file_0.jpg", "B/file_1.jpg", "B/file_2.jpg"}


@pytest.mark.slow
def test_enqueue_by_asset_id(filter_test_env: tuple[str, str, str]) -> None:
    """Enqueue single asset by asset_id, assert count=1."""
    tenant_url, _api_key, _tenant_id = filter_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="AssetId_" + secrets.token_urlsafe(6),
            root_path="/asset-id",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        asset_id = "ast_" + str(ULID())
        session.execute(
            insert(Asset),
            [{
                "asset_id": asset_id,
                "library_id": library_id,
                "rel_path": "single.jpg",
                "file_size": 1000,
                "media_type": "image/jpeg",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            }],
        )
        session.commit()

        spec = AssetFilterSpec(library_id=library_id, asset_id=asset_id)
        n = enqueue_jobs_for_filter(session, spec, "proxy", force=False)
        assert n == 1

        row = session.execute(
            text(
                "SELECT w.asset_id FROM worker_jobs w "
                "JOIN assets a ON a.asset_id = w.asset_id "
                "WHERE w.job_type = 'proxy' AND w.status = 'pending' AND a.library_id = :lib"
            ),
            {"lib": library_id},
        ).fetchone()
        assert row is not None and row[0] == asset_id


@pytest.mark.slow
def test_enqueue_force_cancels_existing(filter_test_env: tuple[str, str, str]) -> None:
    """Enqueue, then enqueue again with force=True; assert old jobs cancelled and new ones created."""
    tenant_url, _api_key, _tenant_id = filter_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="Force_" + secrets.token_urlsafe(6),
            root_path="/force",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        asset_id = "ast_" + str(ULID())
        session.execute(
            insert(Asset),
            [{
                "asset_id": asset_id,
                "library_id": library_id,
                "rel_path": "one.jpg",
                "file_size": 1000,
                "media_type": "image/jpeg",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            }],
        )
        session.commit()

        spec = AssetFilterSpec(library_id=library_id)
        n1 = enqueue_jobs_for_filter(session, spec, "proxy", force=False)
        assert n1 == 1

        cancelled_before = session.execute(
            text("SELECT COUNT(*) FROM worker_jobs WHERE status = 'cancelled'"),
        ).scalar()
        assert cancelled_before == 0

        n2 = enqueue_jobs_for_filter(session, spec, "proxy", force=True)
        assert n2 == 1

        cancelled = session.execute(
            text(
                "SELECT COUNT(*) FROM worker_jobs w "
                "JOIN assets a ON a.asset_id = w.asset_id "
                "WHERE w.status = 'cancelled' AND w.job_type = 'proxy' AND a.library_id = :lib"
            ),
            {"lib": library_id},
        ).scalar()
        assert cancelled == 1
        pending = session.execute(
            text(
                "SELECT COUNT(*) FROM worker_jobs w "
                "JOIN assets a ON a.asset_id = w.asset_id "
                "WHERE w.status = 'pending' AND w.job_type = 'proxy' AND a.library_id = :lib"
            ),
            {"lib": library_id},
        ).scalar()
        assert pending == 1


@pytest.mark.slow
def test_enqueue_missing_proxy(filter_test_env: tuple[str, str, str]) -> None:
    """Set proxy_key on some assets; enqueue with missing_proxy=True; assert only unproxied assets enqueued."""
    tenant_url, _api_key, _tenant_id = filter_test_env

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as session:
        lib_repo = LibraryRepository(session)
        library = lib_repo.create(
            name="MissingProxy_" + secrets.token_urlsafe(6),
            root_path="/missing-proxy",
        )
        library_id = library.library_id

        now = datetime.now(timezone.utc)
        ids_with_proxy = []
        ids_without_proxy = []
        assets = []
        for i in range(4):
            aid = "ast_" + str(ULID())
            has_proxy = i % 2 == 0
            if has_proxy:
                ids_with_proxy.append(aid)
            else:
                ids_without_proxy.append(aid)
            assets.append({
                "asset_id": aid,
                "library_id": library_id,
                "rel_path": f"img_{i}.jpg",
                "file_size": 1000,
                "media_type": "image/jpeg",
                "status": "proxied" if has_proxy else "pending",
                "availability": "online",
                "proxy_key": f"proxy/{aid}.jpg" if has_proxy else None,
                "thumbnail_key": f"thumb/{aid}.jpg" if has_proxy else None,
                "created_at": now,
                "updated_at": now,
            })
        session.execute(insert(Asset), assets)
        session.commit()

        spec = AssetFilterSpec(library_id=library_id, missing_proxy=True)
        n = enqueue_jobs_for_filter(session, spec, "proxy", force=False)
        assert n == 2

        enqueued_ids = [
            r[0]
            for r in session.execute(
                text(
                    "SELECT w.asset_id FROM worker_jobs w "
                    "JOIN assets a ON a.asset_id = w.asset_id "
                    "WHERE w.job_type = 'proxy' AND w.status = 'pending' AND a.library_id = :lib"
                ),
                {"lib": library_id},
            ).fetchall()
        ]
        assert set(enqueued_ids) == set(ids_without_proxy)
