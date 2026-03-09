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
    """Set library.vision_model_id = moondream, complete ai_vision job — assert job payload contains vision_model_id."""
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
    assert library.get("vision_model_id", "moondream") == "moondream"

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
    assert vis_job.get("vision_model_id") == "moondream"


@pytest.mark.slow
def test_patch_library_vision_model_id(caption_slow_env: tuple, tmp_path: Path) -> None:
    """PATCH /v1/libraries/{id} with vision_model_id=qwen, assert GET /v1/libraries returns vision_model_id=qwen."""
    client, api_key, tenant_url = caption_slow_env

    lib_name = "PatchModel_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()
    library_id = library["library_id"]

    patch_r = client.patch(
        f"/v1/libraries/{library_id}",
        json={"vision_model_id": "qwen"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert patch_r.status_code == 200
    assert patch_r.json().get("vision_model_id") == "qwen"

    list_r = client.get("/v1/libraries", headers={"Authorization": f"Bearer {api_key}"})
    assert list_r.status_code == 200
    libs = list_r.json()
    match = next((l for l in libs if l["library_id"] == library_id), None)
    assert match is not None
    assert match["vision_model_id"] == "qwen"


@pytest.mark.slow
def test_patch_library_invalid_model_id(caption_slow_env: tuple, tmp_path: Path) -> None:
    """PATCH /v1/libraries/{id} with vision_model_id=invented, assert 422."""
    client, api_key, tenant_url = caption_slow_env

    lib_name = "InvalidModel_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library_id = r.json()["library_id"]

    patch_r = client.patch(
        f"/v1/libraries/{library_id}",
        json={"vision_model_id": "invented"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert patch_r.status_code == 422


@pytest.mark.slow
def test_vision_worker_qwen_provider_called(caption_slow_env: tuple, tmp_path: Path) -> None:
    """Set library to qwen. Enqueue and claim ai_vision job. Mock QwenCaptionProvider.describe. Complete. Assert model_id=qwen."""
    client, api_key, tenant_url = caption_slow_env
    auth = _AuthClient(client, api_key)

    lib_name = "QwenProvider_" + secrets.token_urlsafe(4)
    r = client.post(
        "/v1/libraries",
        json={"name": lib_name, "root_path": str(tmp_path)},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    library = r.json()
    library_id = library["library_id"]

    patch_r = client.patch(
        f"/v1/libraries/{library_id}",
        json={"vision_model_id": "qwen"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert patch_r.status_code == 200

    img = Image.new("RGB", (6, 6), color=(100, 50, 150))
    img.save(tmp_path / "qwen.jpg", "JPEG")
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
    proxy_key = "t/l/proxies/00/" + asset_id + "_qwen.jpg"
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
    assert vis_job.get("vision_model_id") == "qwen"

    mock_result = {"description": "Mocked Qwen description", "tags": ["mock", "qwen"]}
    with patch(
        "src.workers.captions.qwen_caption.QwenCaptionProvider.describe",
        return_value=mock_result,
    ):
        from src.storage.local import get_storage
        from src.workers.vision_worker import VisionWorker

        storage = get_storage()
        worker = VisionWorker(client=MagicMock(), storage=storage, once=True)
        payload = worker.process(vis_job)
        assert payload["model_id"] == "qwen"
        assert payload["model_version"] == "1"
        assert payload["description"] == mock_result["description"]
        assert payload["tags"] == mock_result["tags"]

    complete_r = client.post(
        f"/v1/jobs/{vis_job['job_id']}/complete",
        json={
            "model_id": "qwen",
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
                "WHERE asset_id = :asset_id AND model_id = 'qwen' AND model_version = '1'"
            ),
            {"asset_id": asset_id},
        ).fetchone()
    engine.dispose()
    assert row is not None
    assert row[0] == "qwen"
    assert row[1] == "1"
    data = _jsonb(row[2])
    assert data["description"] == mock_result["description"]
    assert data["tags"] == mock_result["tags"]
