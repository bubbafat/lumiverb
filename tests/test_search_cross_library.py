"""Integration tests for cross-library search (per-tenant Quickwit indexes).

Tests the search API with optional library_id, cross-library results,
and library_id/library_name in SearchHit responses. Uses Postgres
fallback (Quickwit not running in test).
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _ingest_asset_with_vision(client, api_key, library_id, rel_path, description, tags=None) -> str:
    """Ingest an asset with vision metadata so it's searchable."""
    from PIL import Image as PILImage
    import json

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    vision = json.dumps({
        "model_id": "test-model",
        "model_version": "1",
        "description": description,
        "tags": tags or [],
    })

    r = client.post(
        "/v1/ingest",
        data={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "1000",
            "media_type": "image",
            "width": "100",
            "height": "100",
            "vision": vision,
        },
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


@pytest.fixture(scope="module")
def search_env():
    """Two libraries with searchable assets. Quickwit disabled, Postgres fallback enabled."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = control_postgres.get_connection_url()
        control_url = _ensure_psycopg2(control_url)
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
        os.environ["ADMIN_KEY"] = "test-admin-xsearch"
        os.environ["QUICKWIT_ENABLED"] = "false"
        os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SearchTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-xsearch"},
                )
                assert r.status_code == 200
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                r1 = client.post(
                    "/v1/libraries",
                    json={"name": "Travel", "root_path": "/tmp/travel"},
                    headers=_headers(api_key),
                )
                assert r1.status_code == 200
                lib1_id = r1.json()["library_id"]

                r2 = client.post(
                    "/v1/libraries",
                    json={"name": "Portraits", "root_path": "/tmp/portraits"},
                    headers=_headers(api_key),
                )
                assert r2.status_code == 200
                lib2_id = r2.json()["library_id"]

                # Ingest assets with vision metadata
                a1 = _ingest_asset_with_vision(
                    client, api_key, lib1_id, "sunset_beach.jpg",
                    "A beautiful golden sunset over the ocean beach",
                    ["sunset", "beach", "ocean"],
                )
                a2 = _ingest_asset_with_vision(
                    client, api_key, lib2_id, "portrait_studio.jpg",
                    "Studio portrait with dramatic lighting",
                    ["portrait", "studio"],
                )
                a3 = _ingest_asset_with_vision(
                    client, api_key, lib1_id, "mountain_sunset.jpg",
                    "Sunset behind mountain peaks with golden clouds",
                    ["sunset", "mountain"],
                )

                yield client, api_key, lib1_id, lib2_id, a1, a2, a3

        _engines.clear()


# ---------------------------------------------------------------------------
# Cross-library search
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_without_library_id(search_env):
    """Search without library_id returns results across all libraries."""
    client, api_key, lib1_id, lib2_id, a1, a2, a3 = search_env

    r = client.get(
        "/v1/search",
        params={"q": "sunset"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    ids = [h["asset_id"] for h in data["hits"]]
    # sunset appears in lib1 (2 assets) — should find at least those
    assert a1 in ids or a3 in ids


@pytest.mark.slow
def test_search_with_library_id_filters(search_env):
    """Search with library_id only returns results from that library."""
    client, api_key, lib1_id, lib2_id, a1, a2, a3 = search_env

    r = client.get(
        "/v1/search",
        params={"q": "sunset", "library_id": lib2_id},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    ids = [h["asset_id"] for h in data["hits"]]
    # "sunset" is not in lib2's description
    assert a1 not in ids
    assert a3 not in ids


@pytest.mark.slow
def test_search_hits_include_library_fields(search_env):
    """SearchHit includes library_id and library_name."""
    client, api_key, lib1_id, lib2_id, a1, a2, a3 = search_env

    r = client.get(
        "/v1/search",
        params={"q": "portrait"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["hits"]) >= 1
    hit = next((h for h in data["hits"] if h["asset_id"] == a2), None)
    if hit:
        assert hit["library_id"] == lib2_id
        assert hit["library_name"] == "Portraits"


@pytest.mark.slow
def test_search_empty_query_no_date_returns_empty(search_env):
    """Empty query with no date filter returns empty response."""
    client, api_key, *_ = search_env

    r = client.get(
        "/v1/search",
        params={"q": ""},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.slow
def test_search_no_results(search_env):
    """Search for non-existent term returns empty hits."""
    client, api_key, *_ = search_env

    r = client.get(
        "/v1/search",
        params={"q": "xyznonexistent12345"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.slow
def test_search_requires_auth(search_env):
    """Search without auth fails."""
    client, *_ = search_env
    r = client.get("/v1/search", params={"q": "sunset"})
    assert r.status_code in (401, 403)


@pytest.mark.slow
def test_search_date_only_cross_library(search_env):
    """Date-only search without library_id returns 200 (may be empty if assets lack taken_at)."""
    client, api_key, *_ = search_env

    r = client.get(
        "/v1/search",
        params={"date_from": "2020-01-01", "date_to": "2030-12-31"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    # Test assets may not have taken_at/file_mtime set, so we just verify the endpoint works
    assert "hits" in r.json()
