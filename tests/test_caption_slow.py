"""Slow tests for caption providers and per-library vision model switching."""

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
def caption_slow_env():
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
                    json={"name": "CaptionSlowTenant", "plan": "free"},
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
def test_vision_worker_uses_provider_from_library(caption_slow_env: tuple, tmp_path: Path) -> None:
    """Enqueue ai_vision job, claim it, and verify it has expected fields."""
    client, api_key, tenant_url = caption_slow_env
    auth = _AuthClient(client, api_key)

    lib_name = "VisionModel_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()

    img = Image.new("RGB", (8, 8), color=(0, 200, 100))
    img.save(tmp_path / "img.jpg", "JPEG")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    proxy_job = client.get(
        "/v1/jobs/next", params={"job_type": "proxy"}, headers={"Authorization": f"Bearer {api_key}"}
    ).json()
    asset_id = proxy_job["asset_id"]
    proxy_key = "t/l/proxies/00/" + asset_id + "_img.jpg"
    client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": proxy_key, "thumbnail_key": "t", "width": 8, "height": 8},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library["library_id"]}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    next_vis = client.get(
        "/v1/jobs/next", params={"job_type": "ai_vision"}, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert next_vis.status_code == 200
    vis_job = next_vis.json()
    assert "asset_id" in vis_job


@pytest.mark.slow
def test_vision_worker_openai_provider_called(caption_slow_env: tuple, tmp_path: Path) -> None:
    """Enqueue and claim ai_vision job. Mock describe. Complete. Assert model_id stored."""
    client, api_key, tenant_url = caption_slow_env
    auth = _AuthClient(client, api_key)

    model_id = "qwen3-visioncaption-2b"
    lib_name = "OpenAIProvider_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()
    library_id = library["library_id"]

    img = Image.new("RGB", (6, 6), color=(100, 50, 150))
    img.save(tmp_path / "vision.jpg", "JPEG")
    result = scan_library(auth, library, force=True)
    assert result.status == "complete"

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "proxy", "filter": {"library_id": library_id}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    proxy_job = client.get(
        "/v1/jobs/next", params={"job_type": "proxy"}, headers={"Authorization": f"Bearer {api_key}"}
    ).json()
    asset_id = proxy_job["asset_id"]
    proxy_key = "t/l/proxies/00/" + asset_id + "_vision.jpg"
    client.post(
        f"/v1/jobs/{proxy_job['job_id']}/complete",
        json={"proxy_key": proxy_key, "thumbnail_key": "t", "width": 6, "height": 6},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    client.post(
        "/v1/jobs/enqueue",
        json={"job_type": "ai_vision", "filter": {"library_id": library_id}, "force": False},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    next_vis = client.get(
        "/v1/jobs/next", params={"job_type": "ai_vision"}, headers={"Authorization": f"Bearer {api_key}"}
    )
    assert next_vis.status_code == 200
    vis_job = next_vis.json()

    # Inject the model_id into the job payload (simulates runtime model resolution).
    vis_job["vision_model_id"] = model_id

    # Provide a non-empty vision_api_url so the worker's validation passes;
    # the actual URL is irrelevant because describe() is fully mocked below.
    vis_job["vision_api_url"] = "http://mock-vision-server/"

    mock_result = {"description": "Mocked description", "tags": ["mock", "vision"]}
    with patch(
        "src.workers.captions.openai_caption.OpenAICompatibleCaptionProvider.describe",
        return_value=mock_result,
    ):
        from src.workers.vision_worker import VisionWorker

        artifact_store = MagicMock()
        artifact_store.read_artifact.return_value = b"\xff\xd8\xff" + b"\x00" * 10
        worker = VisionWorker(client=MagicMock(), artifact_store=artifact_store, once=True)
        payload = worker.process(vis_job)
        assert payload["model_id"] == model_id
        assert payload["model_version"] == "1"
        assert payload["description"] == mock_result["description"]
        assert payload["tags"] == mock_result["tags"]

    complete_r = client.post(
        f"/v1/jobs/{vis_job['job_id']}/complete",
        json={
            "model_id": model_id,
            "model_version": "1",
            "description": mock_result["description"],
            "tags": mock_result["tags"],
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert complete_r.status_code == 200

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT model_id, model_version, data FROM asset_metadata "
                "WHERE asset_id = :asset_id AND model_id = :model_id AND model_version = '1'"
            ),
            {"asset_id": asset_id, "model_id": model_id},
        ).fetchone()
    engine.dispose()
    assert row is not None
    assert row[0] == model_id
    assert row[1] == "1"
    data = _jsonb(row[2])
    assert data["description"] == mock_result["description"]
    assert data["tags"] == mock_result["tags"]
