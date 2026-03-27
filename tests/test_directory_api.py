"""API tests for library directory tree endpoint and assets page path_prefix filter."""

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
def directory_api_client() -> tuple[TestClient, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create:
    - one library with nested assets under 2023 and 2024/...
    - one library with only flat assets (no subdirectories).

    Yields (client, api_key, nested_library_id, flat_library_id).
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
                    json={"name": "DirectoryAPITenant", "plan": "free"},
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

                # Library with nested directory structure
                r_lib_nested = client.post(
                    "/v1/libraries",
                    json={"name": "NestedLib", "root_path": "/nested"},
                    headers=auth,
                )
                assert r_lib_nested.status_code == 200
                nested_library_id = r_lib_nested.json()["library_id"]

                r_scan_nested = client.post(
                    "/v1/scans",
                    json={"library_id": nested_library_id, "status": "running"},
                    headers=auth,
                )
                assert r_scan_nested.status_code == 200
                scan_id_nested = r_scan_nested.json()["scan_id"]

                nested_paths = [
                    "2024/Europe/France/Paris/img1.jpg",
                    "2024/Europe/France/Lyon/img2.jpg",
                    "2024/Asia/Japan/img3.jpg",
                    "2023/img4.jpg",
                ]
                for i, rp in enumerate(nested_paths):
                    r_up = client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": nested_library_id,
                            "rel_path": rp,
                            "file_size": 2000 + i,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image/jpeg",
                            "scan_id": scan_id_nested,
                        },
                        headers=auth,
                    )
                    assert r_up.status_code == 200

                # Library with flat assets only (no subdirectories)
                r_lib_flat = client.post(
                    "/v1/libraries",
                    json={"name": "FlatLib", "root_path": "/flat"},
                    headers=auth,
                )
                assert r_lib_flat.status_code == 200
                flat_library_id = r_lib_flat.json()["library_id"]

                r_scan_flat = client.post(
                    "/v1/scans",
                    json={"library_id": flat_library_id, "status": "running"},
                    headers=auth,
                )
                assert r_scan_flat.status_code == 200
                scan_id_flat = r_scan_flat.json()["scan_id"]

                flat_paths = ["a.jpg", "b.png", "c.heic"]
                for i, rp in enumerate(flat_paths):
                    r_up = client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": flat_library_id,
                            "rel_path": rp,
                            "file_size": 3000 + i,
                            "file_mtime": "2025-01-02T12:00:00Z",
                            "media_type": "image/jpeg",
                            "scan_id": scan_id_flat,
                        },
                        headers=auth,
                    )
                    assert r_up.status_code == 200

                yield client, api_key, nested_library_id, flat_library_id

        _engines.clear()


@pytest.mark.slow
def test_directories_root_returns_top_level(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories with no parent returns root directories."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/libraries/{nested_library_id}/directories", headers=auth)
    assert r.status_code == 200
    dirs = r.json()
    names = [d["name"] for d in dirs]
    assert names == ["2023", "2024"]
    counts = {d["name"]: d["asset_count"] for d in dirs}
    assert counts["2023"] == 1
    assert counts["2024"] == 3


@pytest.mark.slow
def test_directories_children_of_2024(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories?parent=2024 returns children of 2024."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        f"/v1/libraries/{nested_library_id}/directories",
        params={"parent": "2024"},
        headers=auth,
    )
    assert r.status_code == 200
    dirs = r.json()
    names = [d["name"] for d in dirs]
    assert names == ["Asia", "Europe"]
    counts = {d["name"]: d["asset_count"] for d in dirs}
    assert counts["Asia"] == 1
    assert counts["Europe"] == 2


@pytest.mark.slow
def test_directories_children_of_2024_europe(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories?parent=2024/Europe returns children of 2024/Europe."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        f"/v1/libraries/{nested_library_id}/directories",
        params={"parent": "2024/Europe"},
        headers=auth,
    )
    assert r.status_code == 200
    dirs = r.json()
    names = [d["name"] for d in dirs]
    assert names == ["France"]
    counts = {d["name"]: d["asset_count"] for d in dirs}
    assert counts["France"] == 2


@pytest.mark.slow
def test_directories_children_of_2024_europe_france(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories?parent=2024/Europe/France returns cities."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        f"/v1/libraries/{nested_library_id}/directories",
        params={"parent": "2024/Europe/France"},
        headers=auth,
    )
    assert r.status_code == 200
    dirs = r.json()
    names = [d["name"] for d in dirs]
    assert names == ["Lyon", "Paris"]
    counts = {d["name"]: d["asset_count"] for d in dirs}
    assert counts["Lyon"] == 1
    assert counts["Paris"] == 1


@pytest.mark.slow
def test_directories_flat_library_returns_empty(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories on flat library returns []."""
    client, api_key, _, flat_library_id = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/libraries/{flat_library_id}/directories", headers=auth)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.slow
def test_directories_unknown_library_404(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories on unknown library_id returns 404."""
    client, api_key, _, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get("/v1/libraries/lib_nonexistent/directories", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_directories_parent_traversal_400(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/libraries/{id}/directories?parent=.. returns 400."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        f"/v1/libraries/{nested_library_id}/directories",
        params={"parent": ".."},
        headers=auth,
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_assets_page_path_prefix_2024(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/assets/page?path_prefix=2024 returns only assets under 2024/."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": nested_library_id, "path_prefix": "2024"},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["rel_path"] for i in items} == {
        "2024/Europe/France/Paris/img1.jpg",
        "2024/Europe/France/Lyon/img2.jpg",
        "2024/Asia/Japan/img3.jpg",
    }


@pytest.mark.slow
def test_assets_page_path_prefix_2024_europe(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/assets/page?path_prefix=2024/Europe returns only assets under 2024/Europe/."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": nested_library_id, "path_prefix": "2024/Europe"},
        headers=auth,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert {i["rel_path"] for i in items} == {
        "2024/Europe/France/Paris/img1.jpg",
        "2024/Europe/France/Lyon/img2.jpg",
    }


@pytest.mark.slow
def test_assets_page_path_prefix_nonexistent_empty(
    directory_api_client: tuple[TestClient, str, str, str]
) -> None:
    """GET /v1/assets/page?path_prefix=nonexistent returns empty items."""
    client, api_key, nested_library_id, _ = directory_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(
        "/v1/assets/page",
        params={"library_id": nested_library_id, "path_prefix": "nonexistent"},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []

