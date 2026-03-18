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
from src.repository.tenant import (
    AssetEmbeddingRepository,
    SearchSyncQueueRepository,
    WorkerJobRepository,
)
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


@pytest.mark.slow
def test_list_failures_excludes_trashed_asset(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        with Session(engine) as session:
            active_asset_id = _insert_asset(session, library_id, f"fail_active_{secrets.token_urlsafe(4)}.jpg")
            trashed_asset_id = _insert_asset(session, library_id, f"fail_trashed_{secrets.token_urlsafe(4)}.jpg")
            repo = WorkerJobRepository(session)
            active_job = repo.create("metadata", active_asset_id)
            trashed_job = repo.create("metadata", trashed_asset_id)
            repo.set_failed(active_job, "active error")
            repo.set_failed(trashed_job, "trashed error")
            rows, total = repo.list_failures(library_id, "metadata")
            assert total == 2

        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)

        with Session(engine) as session:
            repo = WorkerJobRepository(session)
            rows, total = repo.list_failures(library_id, "metadata")
            asset_ids_in_failures = {r["rel_path"] for r in rows}
            assert total == 1
            assert not any(trashed_asset_id in str(r) for r in rows)
    finally:
        engine.dispose()


@pytest.mark.slow
def test_active_worker_count_excludes_trashed_asset(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        # Baseline before we insert anything for this test.
        with Session(engine) as session:
            repo = WorkerJobRepository(session)
            baseline = repo.active_worker_count(library_id=library_id)

        with Session(engine) as session:
            trashed_asset_id = _insert_asset(session, library_id, f"wc_trashed_{secrets.token_urlsafe(4)}.jpg")
            repo = WorkerJobRepository(session)
            repo.create("wc_test", trashed_asset_id)
            worker_id = "w_" + secrets.token_urlsafe(6)
            # claim_next may claim a different pending job; instead inject directly.
            session.execute(
                text("""
                    UPDATE worker_jobs
                    SET status = 'claimed',
                        worker_id = :worker_id,
                        claimed_at = NOW(),
                        lease_expires_at = NOW() + INTERVAL '10 minutes'
                    WHERE asset_id = :asset_id AND job_type = 'wc_test'
                """),
                {"worker_id": worker_id, "asset_id": trashed_asset_id},
            )
            session.commit()
            count_after_claim = repo.active_worker_count(library_id=library_id)
            assert count_after_claim == baseline + 1

        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)

        with Session(engine) as session:
            repo = WorkerJobRepository(session)
            count_after_trash = repo.active_worker_count(library_id=library_id)
            assert count_after_trash == baseline
    finally:
        engine.dispose()


@pytest.mark.slow
def test_search_sync_pipeline_status_excludes_trashed_asset(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    try:
        # Baseline pending count before inserting a new sync entry.
        with Session(engine) as session:
            ssq_repo = SearchSyncQueueRepository(session)
            baseline_by_status = {r["status"]: r["count"] for r in ssq_repo.search_sync_pipeline_status(library_id)}
            baseline_pending = baseline_by_status.get("pending", 0)

        with Session(engine) as session:
            trashed_asset_id = _insert_asset(session, library_id, f"ssync_trashed_{secrets.token_urlsafe(4)}.jpg")
            ssq_repo = SearchSyncQueueRepository(session)
            ssq_repo.enqueue(trashed_asset_id, "index")
            by_status_after_enqueue = {r["status"]: r["count"] for r in ssq_repo.search_sync_pipeline_status(library_id)}
            assert by_status_after_enqueue.get("pending", 0) == baseline_pending + 1

        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)

        with Session(engine) as session:
            ssq_repo = SearchSyncQueueRepository(session)
            by_status_after_trash = {r["status"]: r["count"] for r in ssq_repo.search_sync_pipeline_status(library_id)}
            assert by_status_after_trash.get("pending", 0) == baseline_pending
    finally:
        engine.dispose()


@pytest.mark.slow
def test_find_similar_excludes_trashed_asset(
    trash_worker_client: tuple[TestClient, str, str],
) -> None:
    client, api_key, library_id = trash_worker_client
    auth = {"Authorization": f"Bearer {api_key}"}

    engine = _tenant_engine_from_client(client, api_key)
    vec = [0.1] * 512
    model_id = "test-model"
    model_version = "v1"
    try:
        with Session(engine) as session:
            trashed_asset_id = _insert_asset(session, library_id, f"sim_trashed_{secrets.token_urlsafe(4)}.jpg")
            emb_repo = AssetEmbeddingRepository(session)
            emb_repo.upsert(trashed_asset_id, model_id, model_version, vec)
            results_before = emb_repo.find_similar(library_id, model_id, model_version, vec, limit=10)
            assert any(asset_id == trashed_asset_id for asset_id, _ in results_before)

        client.delete(f"/v1/assets/{trashed_asset_id}", headers=auth)

        with Session(engine) as session:
            emb_repo = AssetEmbeddingRepository(session)
            results_after = emb_repo.find_similar(library_id, model_id, model_version, vec, limit=10)
            assert not any(asset_id == trashed_asset_id for asset_id, _ in results_after)
    finally:
        engine.dispose()
