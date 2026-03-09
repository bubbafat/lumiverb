import os
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.cli.scanner import scan_library
from src.core.config import get_settings
from src.core.database import _engines
from src.repository.control_plane import TenantDbRoutingRepository
from src.repository.tenant import SearchSyncQueueRepository
from src.workers.search_sync import SearchSyncWorker
from tests.conftest import (
    _AuthClient,
    _ensure_psycopg2,
    _provision_tenant_db,
    _run_control_migrations,
)


@pytest.fixture(scope="module")
def search_sync_env():
    """Two Postgres containers; create tenant; yield (TestClient, api_key, tenant_url)."""
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
                    json={"name": "SearchSyncTenant", "plan": "free"},
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

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                yield client, api_key, tenant_url
        _engines.clear()


@pytest.mark.slow
def test_ai_vision_completion_enqueues_search_sync(search_sync_env: tuple, tmp_path: Path) -> None:
    """Completing an ai_vision job should enqueue a search_sync_queue row."""
    client, api_key, tenant_url = search_sync_env
    auth = _AuthClient(client, api_key)

    lib_name = "SearchSyncEnqueue_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    library = r.json()

    img = Image.new("RGB", (10, 10), color=(0, 128, 255))
    img.save(tmp_path / "photo.jpg", "JPEG")

    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    # Complete a proxy job so vision worker can run
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    proxy_job = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()
    asset_id = proxy_job["asset_id"]
    client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": "p", "thumbnail_key": "t", "width": 10, "height": 10},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    # Enqueue and complete ai_vision job
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    vis_job = client.get(
        "/v1/jobs/next",
        params={"job_type": "ai_vision"},
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()
    assert vis_job["asset_id"] == asset_id

    complete_vis = client.post(
        f"/v1/jobs/{vis_job['job_id']}/complete",
        json={
            "model_id": "moondream",
            "model_version": "2",
            "description": "A tiny blue square.",
            "tags": ["blue", "square"],
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert complete_vis.status_code == 200, (complete_vis.status_code, complete_vis.text)

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM search_sync_queue WHERE asset_id = :asset_id"),
            {"asset_id": asset_id},
        ).scalar_one()
    engine.dispose()
    assert count == 1


class _DummyQuickwit:
    def __init__(self) -> None:
        self.enabled = True
        self.ensure_calls: list[str] = []
        self.ingested: list[dict] = []

    def ensure_index_for_library(self, library_id: str) -> None:
        self.ensure_calls.append(library_id)

    def ingest_documents_for_library(self, library_id: str, docs):
        self.ingested.extend(list(docs))


@pytest.mark.slow
def test_search_sync_worker_drains_queue_and_marks_synced(search_sync_env: tuple, tmp_path: Path) -> None:
    """SearchSyncWorker should build docs, send them, and mark rows as synced."""
    client, api_key, tenant_url = search_sync_env
    auth = _AuthClient(client, api_key)

    lib_name = "SearchSyncDrain_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    library = r.json()

    # Create a single asset with AI metadata via the normal flow.
    img = Image.new("RGB", (12, 12), color=(255, 0, 0))
    img.save(tmp_path / "red.jpg", "JPEG")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    proxy_job = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()
    asset_id = proxy_job["asset_id"]
    client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": "p2", "thumbnail_key": "t2", "width": 12, "height": 12},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    vis_job = client.get(
        "/v1/jobs/next",
        params={"job_type": "ai_vision"},
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()
    client.post(
        f"/v1/jobs/{vis_job['job_id']}/complete",
        json={
            "model_id": "moondream",
            "model_version": "2",
            "description": "A bright red square.",
            "tags": ["red", "square"],
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )

    # Verify queue has a pending row.
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        pending = conn.execute(
            text(
                "SELECT COUNT(*) FROM search_sync_queue "
                "WHERE status = 'pending' AND asset_id = :asset_id"
            ),
            {"asset_id": asset_id},
        ).scalar_one()
    assert pending == 1

    dummy_qw = _DummyQuickwit()
    with Session(engine) as session:
        worker = SearchSyncWorker(
            session=session,
            library_id=library["library_id"],
            quickwit=dummy_qw,
            batch_size=10,
        )
        processed = worker.run_once()
        assert processed == 1

        # Queue row should now be marked as synced.
        repo = SearchSyncQueueRepository(session)
        remaining_pending = session.execute(
            text(
                "SELECT COUNT(*) FROM search_sync_queue "
                "WHERE status = 'pending' AND asset_id = :asset_id"
            ),
            {"asset_id": asset_id},
        ).scalar_one()
        synced = session.execute(
            text(
                "SELECT COUNT(*) FROM search_sync_queue "
                "WHERE status = 'synced' AND asset_id = :asset_id"
            ),
            {"asset_id": asset_id},
        ).scalar_one()
        assert remaining_pending == 0
        assert synced == 1

    engine.dispose()

    # Dummy Quickwit should have received one document.
    assert library["library_id"] in dummy_qw.ensure_calls
    assert len(dummy_qw.ingested) == 1
    doc = dummy_qw.ingested[0]
    assert doc["asset_id"] == asset_id
    assert doc["description"] == "A bright red square."
    assert doc["tags"] == ["red", "square"]

