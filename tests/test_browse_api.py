"""Unified browse API tests. Uses testcontainers Postgres + tenant DB."""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _ingest_asset(client, api_key, library_id, rel_path) -> str:
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    r = client.post(
        "/v1/ingest",
        data={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "1000",
            "media_type": "image",
            "width": "100",
            "height": "100",
        },
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def browse_env():
    """Two testcontainers Postgres: control + tenant. Two libraries. Yield (client, api_key, lib1_id, lib2_id)."""
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
        os.environ["ADMIN_KEY"] = "test-admin-browse"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "BrowseTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-browse"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
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
                # Create two libraries
                r1 = client.post(
                    "/v1/libraries",
                    json={"name": "Library Alpha", "root_path": "/tmp/browse-alpha"},
                    headers=_headers(api_key),
                )
                assert r1.status_code == 200
                lib1_id = r1.json()["library_id"]

                r2 = client.post(
                    "/v1/libraries",
                    json={"name": "Library Beta", "root_path": "/tmp/browse-beta"},
                    headers=_headers(api_key),
                )
                assert r2.status_code == 200
                lib2_id = r2.json()["library_id"]

                yield client, api_key, lib1_id, lib2_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Cross-library browse
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_returns_assets_from_all_libraries(browse_env):
    client, api_key, lib1_id, lib2_id = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "alpha1.jpg")
    a2 = _ingest_asset(client, api_key, lib2_id, "beta1.jpg")

    r = client.get("/v1/browse", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    ids = [i["asset_id"] for i in data["items"]]
    assert a1 in ids
    assert a2 in ids

    # Check library_id and library_name are present
    item_map = {i["asset_id"]: i for i in data["items"]}
    assert item_map[a1]["library_id"] == lib1_id
    assert item_map[a1]["library_name"] == "Library Alpha"
    assert item_map[a2]["library_id"] == lib2_id
    assert item_map[a2]["library_name"] == "Library Beta"


@pytest.mark.slow
def test_browse_filter_by_library_id(browse_env):
    client, api_key, lib1_id, lib2_id = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "alpha_filt1.jpg")
    _ingest_asset(client, api_key, lib2_id, "beta_filt1.jpg")

    r = client.get(f"/v1/browse?library_id={lib1_id}", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    # All returned items should be from lib1
    for item in r.json()["items"]:
        assert item["library_id"] == lib1_id


@pytest.mark.slow
def test_browse_path_prefix_without_library_id_rejected(browse_env):
    client, api_key, _, _ = browse_env
    r = client.get("/v1/browse?path_prefix=photos", headers=_headers(api_key))
    assert r.status_code == 400
    assert "library_id" in r.json()["detail"].lower()


@pytest.mark.slow
def test_browse_path_prefix_with_library_id(browse_env):
    client, api_key, lib1_id, _ = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "subdir/nested.jpg")
    a2 = _ingest_asset(client, api_key, lib1_id, "other/file.jpg")

    r = client.get(
        f"/v1/browse?library_id={lib1_id}&path_prefix=subdir",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


# ---------------------------------------------------------------------------
# Rating filters on browse
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_filter_favorite(browse_env):
    client, api_key, lib1_id, lib2_id = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "br_fav1.jpg")
    a2 = _ingest_asset(client, api_key, lib2_id, "br_fav2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))

    r = client.get("/v1/browse?favorite=true", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


@pytest.mark.slow
def test_browse_filter_star_min(browse_env):
    client, api_key, lib1_id, lib2_id = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "br_star1.jpg")
    a2 = _ingest_asset(client, api_key, lib2_id, "br_star2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"stars": 5}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a2}/rating", json={"stars": 2}, headers=_headers(api_key))

    r = client.get("/v1/browse?star_min=4", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


@pytest.mark.slow
def test_browse_filter_color(browse_env):
    client, api_key, lib1_id, lib2_id = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "br_col1.jpg")
    a2 = _ingest_asset(client, api_key, lib2_id, "br_col2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"color": "green"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a2}/rating", json={"color": "blue"}, headers=_headers(api_key))

    r = client.get("/v1/browse?color=green", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_pagination(browse_env):
    client, api_key, lib1_id, _ = browse_env

    # Ingest 3 assets
    assets = []
    for i in range(3):
        assets.append(_ingest_asset(client, api_key, lib1_id, f"page_{i}.jpg"))

    # Page with limit=2
    r = client.get("/v1/browse?limit=2&sort=asset_id&dir=asc", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert data["next_cursor"] is not None

    # Second page
    r2 = client.get(
        f"/v1/browse?limit=2&sort=asset_id&dir=asc&after={data['next_cursor']}",
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    page2_ids = [i["asset_id"] for i in r2.json()["items"]]
    page1_ids = [i["asset_id"] for i in data["items"]]
    # No overlap
    assert not set(page1_ids) & set(page2_ids)


# ---------------------------------------------------------------------------
# EXIF filters
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_media_type_filter(browse_env):
    """media_type filter works on browse."""
    client, api_key, lib1_id, _ = browse_env
    a1 = _ingest_asset(client, api_key, lib1_id, "br_mt1.jpg")

    r = client.get("/v1/browse?media_type=image", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids

    r2 = client.get("/v1/browse?media_type=video", headers=_headers(api_key))
    assert r2.status_code == 200
    # a1 is an image, shouldn't appear in video-only filter
    # (other tests may have created images too, just verify no error)
    ids2 = [i["asset_id"] for i in r2.json()["items"]]
    assert a1 not in ids2


@pytest.mark.slow
def test_browse_no_auth_fails(browse_env):
    """Browse requires authentication."""
    client, _, _, _ = browse_env
    r = client.get("/v1/browse")
    assert r.status_code in (401, 403)
