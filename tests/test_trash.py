"""Tests for asset soft delete (trash/restore) and empty trash API."""

import os
from pathlib import Path
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
def trash_api_client() -> tuple[TestClient, str, str, list[str]]:
    """Control + tenant DB; one tenant, one library, three assets. Yields (client, api_key, library_id, asset_ids)."""
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
                    json={"name": "TrashTenant", "plan": "free"},
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
                    json={"name": "TrashLib", "root_path": "/photos"},
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

                asset_ids: list[str] = []
                for i, rel_path in enumerate(["x.jpg", "y.jpg", "z.png"]):
                    r_up = client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rel_path,
                            "file_size": 1000 + i,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image/jpeg" if "jpg" in rel_path else "image/png",
                            "scan_id": scan_id,
                        },
                        headers=auth,
                    )
                    assert r_up.status_code == 200
                    r_by_path = client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": rel_path},
                        headers=auth,
                    )
                    assert r_by_path.status_code == 200
                    asset_ids.append(r_by_path.json()["asset_id"])

                yield client, api_key, library_id, asset_ids

        _engines.clear()


@pytest.mark.slow
def test_trash_asset_204(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/assets/{id} returns 204; then GET returns 404."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[0]

    r = client.delete(f"/v1/assets/{aid}", headers=auth)
    assert r.status_code == 204

    r_get = client.get(f"/v1/assets/{aid}", headers=auth)
    assert r_get.status_code == 404


@pytest.mark.slow
def test_trash_asset_404_not_found(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/assets/{id} with non-existent id returns 404."""
    client, api_key, _library_id, _asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.delete("/v1/assets/ast_nonexistent999", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_trash_asset_404_already_trashed(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/assets/{id} twice: first 204, second 404. Use asset_ids[1] to avoid fixture ordering."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[1]

    r1 = client.delete(f"/v1/assets/{aid}", headers=auth)
    assert r1.status_code == 204

    r2 = client.delete(f"/v1/assets/{aid}", headers=auth)
    assert r2.status_code == 404


@pytest.mark.slow
def test_restore_asset_204(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """Trash then POST restore returns 204; GET then returns 200."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[0]

    client.delete(f"/v1/assets/{aid}", headers=auth)
    r_restore = client.post(f"/v1/assets/{aid}/restore", headers=auth)
    assert r_restore.status_code == 204

    r_get = client.get(f"/v1/assets/{aid}", headers=auth)
    assert r_get.status_code == 200
    assert r_get.json()["asset_id"] == aid


@pytest.mark.slow
def test_restore_asset_404_not_trashed(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """POST /v1/assets/{id}/restore on non-trashed asset returns 404. Use asset_ids[2] to avoid fixture ordering."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[2]

    r = client.post(f"/v1/assets/{aid}/restore", headers=auth)
    assert r.status_code == 404


@pytest.mark.slow
def test_batch_trash(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/assets with body returns trashed and not_found."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    existing, other = asset_ids[0], asset_ids[1]
    nonexistent = "ast_00000000000000000000000000"

    r = client.delete(
        "/v1/assets",
        json={"asset_ids": [existing, nonexistent, other]},
        headers=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert set(data["trashed"]) == {existing, other}
    assert data["not_found"] == [nonexistent]


@pytest.mark.slow
def test_empty_trash_requires_admin(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/trash/empty with member key returns 403."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth_admin = {"Authorization": f"Bearer {api_key}"}

    r_create = client.post(
        "/v1/keys",
        json={"label": "member", "role": "member"},
        headers=auth_admin,
    )
    assert r_create.status_code == 200
    member_plaintext = r_create.json()["plaintext"]
    auth_member = {"Authorization": f"Bearer {member_plaintext}"}

    client.delete(f"/v1/assets/{asset_ids[0]}", headers=auth_admin)

    r = client.delete("/v1/trash/empty", json={}, headers=auth_member)
    assert r.status_code == 403


@pytest.mark.slow
def test_empty_trash_returns_count(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """DELETE /v1/trash/empty with admin key returns { deleted: N }."""
    client, api_key, _library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    for aid in asset_ids[:2]:
        client.delete(f"/v1/assets/{aid}", headers=auth)

    r = client.delete("/v1/trash/empty", json={}, headers=auth)
    assert r.status_code == 200
    assert r.json()["deleted"] == 2


@pytest.mark.slow
def test_trashed_asset_absent_from_list(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """After trash, asset does not appear in GET /v1/assets."""
    client, api_key, library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[0]

    r_before = client.get("/v1/assets", params={"library_id": library_id}, headers=auth)
    assert r_before.status_code == 200
    ids_before = {a["asset_id"] for a in r_before.json()}
    assert aid in ids_before

    client.delete(f"/v1/assets/{aid}", headers=auth)

    r_after = client.get("/v1/assets", params={"library_id": library_id}, headers=auth)
    assert r_after.status_code == 200
    ids_after = {a["asset_id"] for a in r_after.json()}
    assert aid not in ids_after


@pytest.mark.slow
def test_trashed_asset_absent_from_search(trash_api_client: tuple[TestClient, str, str, list[str]]) -> None:
    """Trashed asset does not appear in search results (postgres fallback uses active_assets)."""
    client, api_key, library_id, asset_ids = trash_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = asset_ids[0]

    # Use postgres fallback by disabling Quickwit
    orig = os.environ.get("QUICKWIT_ENABLED")
    try:
        os.environ["QUICKWIT_ENABLED"] = "false"
        get_settings.cache_clear()

        r_before = client.get(
            "/v1/search",
            params={"library_id": library_id, "q": "jpg"},
            headers=auth,
        )
        assert r_before.status_code == 200
        hits_before = {h["asset_id"] for h in r_before.json()["hits"]}

        client.delete(f"/v1/assets/{aid}", headers=auth)

        r_after = client.get(
            "/v1/search",
            params={"library_id": library_id, "q": "jpg"},
            headers=auth,
        )
        assert r_after.status_code == 200
        hits_after = {h["asset_id"] for h in r_after.json()["hits"]}
        assert aid not in hits_after
    finally:
        if orig is not None:
            os.environ["QUICKWIT_ENABLED"] = orig
        else:
            os.environ.pop("QUICKWIT_ENABLED", None)
        get_settings.cache_clear()
