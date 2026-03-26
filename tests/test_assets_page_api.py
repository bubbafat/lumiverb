"""API tests for GET /v1/assets/page (keyset pagination)."""

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

                r_scan = client.post(
                    "/v1/scans",
                    json={"library_id": library_id, "status": "running"},
                    headers=auth,
                )
                assert r_scan.status_code == 200
                scan_id = r_scan.json()["scan_id"]

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
                            "media_type": "image/jpeg" if rp.endswith(".jpg") else "image/png",
                            "scan_id": scan_id,
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
    """GET /v1/assets/page?library_id=...&limit=3 returns exactly 3 items."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 3},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 3
    for item in items:
        assert "asset_id" in item
        assert "rel_path" in item
        assert "file_size" in item
        assert "file_mtime" in item
        assert "media_type" in item


@pytest.mark.slow
def test_page_assets_cursor_pagination(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """Use after cursor from first page to fetch next; assert no overlap and correct total."""
    client, api_key, library_id, _ = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r1 = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "limit": 3},
        headers=auth,
    )
    assert r1.status_code == 200
    page1 = r1.json()
    assert len(page1) == 3
    last_id = page1[-1]["asset_id"]

    r2 = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "after": last_id, "limit": 10},
        headers=auth,
    )
    assert r2.status_code == 200
    page2 = r2.json()

    ids1 = {i["asset_id"] for i in page1}
    ids2 = {i["asset_id"] for i in page2}
    assert ids1.isdisjoint(ids2), "No overlap between pages"
    assert len(page1) + len(page2) == 5, "Both pages together cover all 5 assets"


@pytest.mark.slow
def test_page_assets_204_when_exhausted(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page?after={last_asset_id} returns 204 when no more assets."""
    client, api_key, library_id, asset_ids = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    last_id = asset_ids[-1]
    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "after": last_id, "limit": 10},
        headers=auth,
    )
    assert r.status_code == 204


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
    items = r.json()
    assert len(items) <= 500
    assert len(items) == 5


@pytest.mark.slow
def test_page_assets_missing_vision_returns_all_unprocessed(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page?missing_vision=true returns assets without vision descriptions."""
    client, api_key, library_id, asset_ids = page_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # All 5 assets were created without vision data, so missing_vision should return all of them
    r = client.get(
        "/v1/assets/page",
        params={"library_id": library_id, "missing_vision": "true"},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 5
    returned_ids = {i["asset_id"] for i in items}
    assert returned_ids == set(asset_ids)


@pytest.mark.slow
def test_page_assets_empty_library_204(
    page_api_client: tuple[TestClient, str, str, list[str]]
) -> None:
    """GET /v1/assets/page on library with no assets returns 204."""
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
    assert r.status_code == 204
