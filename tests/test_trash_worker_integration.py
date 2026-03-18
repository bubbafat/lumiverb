"""Slow integration tests: trashed assets excluded from worker job pipeline."""

import os
import secrets
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from src.models.tenant import Asset
from src.repository.tenant import WorkerJobRepository
from src.workers.enqueue import enqueue_proxy_jobs
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def trash_worker_client() -> tuple[TestClient, str, str]:
    """
    Control + tenant DB; one tenant, one library, two assets.
    Yields (client, api_key, library_id).
    """
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
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "TrashWorkerTenant_" + secrets.token_urlsafe(6), "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

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

            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "TrashWorkerLib", "root_path": "/photos"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


def _tenant_engine_from_client(client: TestClient, api_key: str):
    """Resolve tenant DB connection string from API key via control plane routing."""
    from src.core.database import get_control_session
    from src.repository.control_plane import TenantDbRoutingRepository

    auth = {"Authorization": f"Bearer {api_key}"}
    ctx = client.get("/v1/tenant/context", headers=auth).json()
    tenant_id = ctx["tenant_id"]
    with get_control_session() as session:
        routing_repo = TenantDbRoutingRepository(session)
        row = routing_repo.get_by_tenant_id(tenant_id)
        assert row is not None
        tenant_url = row.connection_string
    return create_engine(_ensure_psycopg2(tenant_url))

def _insert_asset(session: Session, library_id: str, rel_path: str) -> str:
    now = datetime.now(timezone.utc)
    asset_id = "ast_" + secrets.token_urlsafe(8)
    session.execute(
        insert(Asset),
        [
            {
                "asset_id": asset_id,
                "library_id": library_id,
                "rel_path": rel_path,
                "file_size": 1000,
                "media_type": "image/jpeg",
                "status": "pending",
                "availability": "online",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    session.commit()
    return asset_id


@pytest.mark.slow
def test_trash_excludes_from_pending_count_claim_and_pipeline_status(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        with Session(engine) as session:
            active_asset_id = _insert_asset(session, library_id, f"active_{secrets.token_urlsafe(4)}.jpg")
            trashed_asset_id = _insert_asset(session, library_id, f"trashed_{secrets.token_urlsafe(4)}.jpg")
            repo = WorkerJobRepository(session)
            repo.create("proxy", active_asset_id)
            repo.create("proxy", trashed_asset_id)
            assert repo.pending_count("proxy", library_id=library_id) == 2

        r = client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)
        assert r.status_code == 204

        with Session(engine) as session:
            repo = WorkerJobRepository(session)
            assert repo.pending_count("proxy", library_id=library_id) == 1

            job = repo.claim_next(
                job_type="proxy",
                worker_id="w_" + secrets.token_urlsafe(6),
                lease_minutes=10,
                library_id=library_id,
            )
            assert job is not None
            assert job.asset_id == active_asset_id

            rows = repo.pipeline_status(library_id)
            by_status = {(r["job_type"], r["status"]): r["count"] for r in rows}
            assert by_status.get(("proxy", "claimed")) == 1
    finally:
        engine.dispose()


@pytest.mark.slow
def test_enqueue_proxy_jobs_does_not_enqueue_for_trashed_asset(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        with Session(engine) as session:
            trashed_asset_id = _insert_asset(session, library_id, f"enqueue_trashed_{secrets.token_urlsafe(4)}.jpg")
        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)
        with Session(engine) as session:
            enqueue_proxy_jobs(session, library_id)
            n = session.execute(
                text(
                    "SELECT COUNT(*)::int FROM worker_jobs WHERE asset_id = :aid AND job_type = 'proxy'"
                ),
                {"aid": trashed_asset_id},
            ).scalar()
            assert int(n or 0) == 0
    finally:
        engine.dispose()


@pytest.mark.slow
def test_list_jobs_library_filter_excludes_trashed_assets(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        with Session(engine) as session:
            active_asset_id = _insert_asset(session, library_id, f"list_active_{secrets.token_urlsafe(4)}.jpg")
            trashed_asset_id = _insert_asset(session, library_id, f"list_trashed_{secrets.token_urlsafe(4)}.jpg")
            repo = WorkerJobRepository(session)
            repo.create("proxy", active_asset_id)
            repo.create("proxy", trashed_asset_id)

        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)

        r_jobs = client.get("/v1/jobs", params={"library_id": library_id}, headers=auth)
        assert r_jobs.status_code == 200
        jobs = r_jobs.json()
        returned_asset_ids = {j["asset_id"] for j in jobs}
        assert trashed_asset_id not in returned_asset_ids
        assert active_asset_id in returned_asset_ids
    finally:
        engine.dispose()

