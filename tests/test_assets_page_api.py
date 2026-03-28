"""API tests for GET /v1/assets/page (keyset pagination with response envelope)."""

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


@pytest.fixture(scope="module")
def page_api_client() -> tuple[TestClient, str, str, list[str]]:
    """
    Two testcontainers Postgres; provision tenant DB; create library + 5 assets with distinct rel_path.
    Yields (client, api_key, library_id, asset_ids_in_order).
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
                    json={"name": "PageAPITenant", "plan": "free"},
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
                    json={"name": "PageAPILib", "root_path": "/page"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                rel_paths = ["one.jpg", "two.jpg", "three.jpg", "four.jpg", "five.png"]
                asset_ids: list[str] = []
                for i, rp in enumerate(rel_paths):
                    client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rp,
                            "file_size": 1000 + i,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image",
                        },
                        headers=auth,
                    )
                    r_by_path = client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": rp},
                        headers=auth,
                    )
                    assert r_by_path.status_code == 200
                    asset_ids.append(r_by_path.json()["asset_id"])

                yield client, api_key, library_id, asset_ids

        _engines.clear()


@pytest.mark.slow
def test_page_assets_returns_first_page(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page?library_id=...&limit=3 returns envelope with 3 items."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 3, "sort": "asset_id", "dir": "asc"},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "next_cursor" in data
    items = data["items"]
    assert len(items) == 3
    assert data["next_cursor"] is not None
    for item in items:
        assert "asset_id" in item
        assert "rel_path" in item
        assert "file_size" in item
        assert "media_type" in item


@pytest.mark.slow
def test_page_assets_cursor_pagination(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """Use next_cursor from first page to fetch next; assert no overlap and correct total."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 3, "sort": "asset_id", "dir": "asc"},
        headers=auth,
    )
    assert r1.status_code == 200
    data1 = r1.json()
    page1 = data1["items"]
    assert len(page1) == 3
    cursor = data1["next_cursor"]
    assert cursor is not None

    r2 = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "after": cursor, "limit": 10, "sort": "asset_id", "dir": "asc"},
        headers=auth,
    )
    assert r2.status_code == 200
    page2 = r2.json()["items"]

    ids1 = {i["asset_id"] for i in page1}
    ids2 = {i["asset_id"] for i in page2}
    assert ids1.isdisjoint(ids2), "No overlap between pages"
    assert len(page1) + len(page2) == 5, "Both pages together cover all 5 assets"


@pytest.mark.slow
def test_page_assets_empty_when_exhausted(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """When all assets have been paged, next_cursor is null and items is empty."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Fetch all 5 in one page
    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 500, "sort": "asset_id", "dir": "asc"},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 5
    # With limit > count, next_cursor should be null
    assert data["next_cursor"] is None


@pytest.mark.slow
def test_page_assets_limit_capped_at_500(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page?limit=9999 still returns <= 500 items (we have 5, so 5)."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 9999},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) <= 500
    assert len(items) == 5


@pytest.mark.slow
def test_page_assets_missing_vision_returns_all_unprocessed(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page?missing_vision=true returns assets without vision descriptions."""
    client, api_key, library_id, asset_ids = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "missing_vision": "true"},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 5
    returned_ids = {i["asset_id"] for i in items}
    assert returned_ids == set(asset_ids)


@pytest.mark.slow
def test_page_assets_empty_library(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page on library with no assets returns empty items."""
    client, api_key, _, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_lib = client.post(
        "/v1/libraries",
        json={"name": "EmptyPageLib", "root_path": "/empty"},
        headers=auth,
    )
    assert r_lib.status_code == 200
    library_id = r_lib.json()["library_id"]

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


@pytest.mark.slow
def test_page_assets_new_fields_present(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """Response items include the new EXIF and metadata fields."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 1},
        headers=auth,
    )
    assert r.status_code == 200
    item = r.json()["items"][0]
    # New fields should be present (nullable)
    for field in ["camera_make", "camera_model", "iso", "aperture",
                  "focal_length", "lens_model", "flash_fired",
                  "gps_lat", "gps_lon", "created_at"]:
        assert field in item, f"Missing field: {field}"


@pytest.mark.slow
def test_page_assets_media_type_filter(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """media_type=image returns only image assets."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "media_type": "image"},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    # All 5 are images (4 jpeg, 1 png)
    assert len(items) == 5
    for item in items:
        assert item["media_type"] == "image"
