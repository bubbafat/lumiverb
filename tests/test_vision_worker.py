import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.cli.scanner import scan_library
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import (
    _AuthClient,
    _ensure_psycopg2,
    _provision_tenant_db,
    _run_control_migrations,
)


@pytest.fixture(scope="module")
def vision_worker_env():
    """Two testcontainers Postgres; create tenant; yield (TestClient, api_key, tenant_url)."""
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
                    json={"name": "VisionWorkerTenant", "plan": "free"},
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

            with TestClient(app) as client:
                yield client, api_key, tenant_url
        _engines.clear()


def _jsonb(val: object) -> object:
    if isinstance(val, str):
        return json.loads(val)
    return val


@pytest.mark.slow
def test_ai_vision_job_complete_stores_metadata(vision_worker_env: tuple, tmp_path: Path) -> None:
    client, api_key, tenant_url = vision_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "VisionStore_" + secrets.token_urlsafe(4)
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

    # Set proxy_key/thumbnail_key by completing a proxy job (no actual proxy files needed here)
    enq_proxy = client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq_proxy.status_code == 200
    next_proxy = client.get(
        "/v1/jobs/next",
        params={"job_type": "proxy"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_proxy.status_code == 200
    proxy_job = next_proxy.json()
    asset_id = proxy_job["asset_id"]

    proxy_key = "t/l/proxies/00/" + asset_id + "_photo.jpg"
    thumb_key = "t/l/thumbnails/00/" + asset_id + "_photo.jpg"
    complete_proxy = client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": proxy_key, "thumbnail_key": thumb_key, "width": 10, "height": 10},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert complete_proxy.status_code == 200, (complete_proxy.status_code, complete_proxy.text)

    enq_vis = client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq_vis.status_code == 200
    assert enq_vis.json().get("enqueued", 0) == 1

    next_vis = client.get(
        "/v1/jobs/next",
        params={"job_type": "ai_vision"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_vis.status_code == 200
    vis_job = next_vis.json()
    assert vis_job["asset_id"] == asset_id
    assert vis_job.get("proxy_key") == proxy_key
    assert vis_job.get("thumbnail_key") == thumb_key

    complete_vis = client.post(
        f"/v1/jobs/{vis_job['job_id']}/complete",
        json={
            "model_id": "moondream",
            "model_version": "2",
            "description": "A blue square.",
            "tags": ["blue", "square"],
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert complete_vis.status_code == 200, (complete_vis.status_code, complete_vis.text)

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT model_id, model_version, data FROM asset_metadata "
                "WHERE asset_id = :asset_id AND model_id = 'moondream' AND model_version = '2'"
            ),
            {"asset_id": asset_id},
        ).fetchone()
    engine.dispose()
    assert row is not None
    assert row[0] == "moondream"
    assert row[1] == "2"
    data = _jsonb(row[2])
    assert data["description"] == "A blue square."
    assert data["tags"] == ["blue", "square"]


@pytest.mark.slow
def test_ai_vision_job_complete_upsert(vision_worker_env: tuple, tmp_path: Path) -> None:
    client, api_key, tenant_url = vision_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "VisionUpsert_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    img = Image.new("RGB", (12, 12), color=(255, 0, 0))
    img.save(tmp_path / "red.jpg", "JPEG")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    # Complete proxy once to set proxy_key (required by vision worker).
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    proxy_job = client.get(
        "/v1/jobs/next", params={"job_type": "proxy"}, headers={"Authorization": f"Bearer {api_key}"}
    ).json()
    asset_id = proxy_job["asset_id"]
    client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": "p", "thumbnail_key": "t", "width": 12, "height": 12},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    # First completion
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    job1 = client.get(
        "/v1/jobs/next", params={"job_type": "ai_vision"}, headers={"Authorization": f"Bearer {api_key}"}
    ).json()
    client.post(
        f"/v1/jobs/{job1['job_id']}/complete",
        json={"model_id": "moondream", "model_version": "2", "description": "First", "tags": ["one"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    # Second completion (force enqueue to re-run and trigger upsert)
    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    job2 = client.get(
        "/v1/jobs/next", params={"job_type": "ai_vision"}, headers={"Authorization": f"Bearer {api_key}"}
    ).json()
    client.post(
        f"/v1/jobs/{job2['job_id']}/complete",
        json={"model_id": "moondream", "model_version": "2", "description": "Second", "tags": ["two"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM asset_metadata "
                "WHERE asset_id = :asset_id AND model_id = 'moondream' AND model_version = '2'"
            ),
            {"asset_id": asset_id},
        ).scalar_one()
        row = conn.execute(
            text(
                "SELECT data FROM asset_metadata "
                "WHERE asset_id = :asset_id AND model_id = 'moondream' AND model_version = '2'"
            ),
            {"asset_id": asset_id},
        ).fetchone()
    engine.dispose()
    assert count == 1
    assert row is not None
    data = _jsonb(row[0])
    assert data["description"] == "Second"
    assert data["tags"] == ["two"]


@pytest.mark.slow
def test_missing_ai_filter(vision_worker_env: tuple, tmp_path: Path) -> None:
    client, api_key, tenant_url = vision_worker_env
    auth = _AuthClient(client, api_key)

    lib_name = "VisionFilter_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    Image.new("RGB", (5, 5), color=(0, 0, 0)).save(tmp_path / "a.jpg", "JPEG")
    Image.new("RGB", (5, 5), color=(255, 255, 255)).save(tmp_path / "b.jpg", "JPEG")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    assets = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    ).json()
    assert len(assets) == 2
    asset_with_meta = assets[0]["asset_id"]
    asset_without_meta = assets[1]["asset_id"]

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, generated_at, data) "
                "VALUES (:metadata_id, :asset_id, :model_id, :model_version, :generated_at, CAST(:data AS JSONB))"
            ),
            {
                "metadata_id": "meta_" + secrets.token_urlsafe(8),
                "asset_id": asset_with_meta,
                "model_id": "moondream",
                "model_version": "1",
                "generated_at": datetime.now(timezone.utc),
                "data": json.dumps({"description": "already", "tags": []}),
            },
        )
        conn.commit()
    engine.dispose()

    enq = client.post(
        "/v1/jobs/enqueue",
        json={
            "job_type": "ai_vision",
            "filter": {"library_id": library["library_id"], "missing_ai": True},
            "force": False,
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert enq.status_code == 200, (enq.status_code, enq.text)
    assert enq.json().get("enqueued", 0) == 1

    next_vis = client.get(
        "/v1/jobs/next",
        params={"job_type": "ai_vision"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert next_vis.status_code == 200
    job = next_vis.json()
    assert job["asset_id"] == asset_without_meta

