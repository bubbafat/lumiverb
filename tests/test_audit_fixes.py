"""Tests for audit fixes implemented during the audit session.

FIX-1 (BUG-1): Stale proxy/thumbnail returns 404 and clears key
FIX-2 (BUG-2): AssetResponse includes duration_sec
FIX-4 (BUG-4): Library trash soft-deletes assets
FIX-6 (BUG-7): video-vision complete does NOT enqueue asset-level search sync
FIX-8 (RISK-1): duration_sec consolidation (duration_ms removed)

FIX-10 (RISK-3): Quickwit ingest failure resets rows to pending
FIX-12 (BUG-8): Scan update clears stale artifact keys
"""

from __future__ import annotations

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

from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---------------------------------------------------------------------------
# Module-scoped fixture: control DB + tenant DB + one tenant + one library
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def audit_fixes_client() -> tuple[TestClient, str, str, str]:
    """
    Two testcontainers Postgres instances; provision tenant DB; create tenant and library.
    Yields (client, api_key, library_id, tenant_url).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_postgres:
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

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "AuditFixesTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
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
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "AuditFixesLib", "root_path": "/audit"},
                    headers=auth,
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id, tenant_url

        _engines.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_asset(
    client: TestClient,
    auth: dict,
    library_id: str,
    rel_path: str,
    media_type: str = "image",
) -> str:
    """Upsert an asset, return its asset_id."""
    r_up = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": media_type,
        },
        headers=auth,
    )
    assert r_up.status_code == 200, (r_up.status_code, r_up.text)

    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
        headers=auth,
    )
    assert r_asset.status_code == 200, (r_asset.status_code, r_asset.text)
    return r_asset.json()["asset_id"]


def _set_proxy_key(tenant_url: str, asset_id: str, proxy_key: str) -> None:
    """Directly set an asset's proxy_key via raw SQL."""
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE assets SET proxy_key = :key WHERE asset_id = :asset_id"),
                {"key": proxy_key, "asset_id": asset_id},
            )
            conn.commit()
    finally:
        engine.dispose()


def _set_thumbnail_key(tenant_url: str, asset_id: str, thumbnail_key: str) -> None:
    """Directly set an asset's thumbnail_key via raw SQL."""
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE assets SET thumbnail_key = :key WHERE asset_id = :asset_id"),
                {"key": thumbnail_key, "asset_id": asset_id},
            )
            conn.commit()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# FIX-1 (BUG-1): a stale proxy/thumbnail is a 404, and the GET leaves the key
# (upkeep repairs it: src/server/missing_artifacts.py)
# ---------------------------------------------------------------------------


def _key(tenant_url: str, asset_id: str, column: str) -> str | None:
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT {column} FROM assets WHERE asset_id = :aid"), {"aid": asset_id}).scalar()
    finally:
        engine.dispose()


@pytest.mark.slow
@pytest.mark.parametrize("public", [False, True], ids=["signed_in", "public_visitor"])
def test_stale_proxy_is_a_404_that_changes_nothing(
    audit_fixes_client: tuple[TestClient, str, str, str], public: bool,
) -> None:
    """GET /artifacts/proxy for a stale proxy_key: 404 artifact_missing; the key stays."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, f"stale_proxy_{public}.jpg")
    _set_proxy_key(tenant_url, asset_id, "nonexistent/fake_proxy.jpg")
    if public:
        assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth).status_code == 200
    try:
        r = (client.get(f"/v1/assets/{asset_id}/artifacts/proxy", params={"public_library_id": library_id})
             if public else client.get(f"/v1/assets/{asset_id}/artifacts/proxy", headers=auth))
    finally:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=auth)
    assert r.status_code == 404, (r.status_code, r.text)
    assert r.json()["error"]["code"] == "artifact_missing"
    assert _key(tenant_url, asset_id, "proxy_key") == "nonexistent/fake_proxy.jpg"


@pytest.mark.slow
def test_stale_thumbnail_is_a_404_that_changes_nothing(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /artifacts/thumbnail for a stale thumbnail_key: 404; the key stays."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "stale_thumb_image.jpg", "image")
    _set_thumbnail_key(tenant_url, asset_id, "nonexistent/fake_thumb.jpg")

    r = client.get(f"/v1/assets/{asset_id}/artifacts/thumbnail", headers=auth)
    assert r.status_code == 404, (r.status_code, r.text)
    assert _key(tenant_url, asset_id, "thumbnail_key") == "nonexistent/fake_thumb.jpg"


# ---------------------------------------------------------------------------
# FIX-2 (BUG-2): AssetResponse includes duration_sec
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_asset_response_includes_duration_sec(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /assets/{id} returns duration_sec when it is set on the asset."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "duration_test.mp4", "video")

    # Set duration_sec directly via SQL
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE assets SET duration_sec = 1.5 WHERE asset_id = :asset_id"),
                {"asset_id": asset_id},
            )
            conn.commit()
    finally:
        engine.dispose()

    r = client.get(f"/v1/assets/{asset_id}", headers=auth)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert "duration_sec" in body, f"duration_sec missing from AssetResponse: {list(body.keys())}"
    assert body["duration_sec"] == pytest.approx(1.5), f"Expected 1.5, got {body['duration_sec']}"


@pytest.mark.slow
def test_asset_detail_duration_sec_from_duration_sec(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /assets/{id} returns correct duration_sec value when set."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "duration_check.mp4", "video")

    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("UPDATE assets SET duration_sec = 90.25 WHERE asset_id = :asset_id"),
                {"asset_id": asset_id},
            )
            conn.commit()
    finally:
        engine.dispose()

    r = client.get(f"/v1/assets/{asset_id}", headers=auth)
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["duration_sec"] == pytest.approx(90.25)


# ---------------------------------------------------------------------------
# FIX-4 (BUG-4): Library trash soft-deletes assets
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_library_trash_soft_deletes_assets(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """DELETE /libraries/{id} sets deleted_at on all assets in the library."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a dedicated library with two assets
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "TrashSoftDeleteLib", "root_path": "/trash-sd"},
        headers=auth,
    )
    assert r_lib.status_code == 200
    test_library_id = r_lib.json()["library_id"]

    asset_id_1 = _upsert_asset(client, auth, test_library_id, "asset1.jpg")
    asset_id_2 = _upsert_asset(client, auth, test_library_id, "asset2.jpg")

    # Trash the library
    r_del = client.delete(f"/v1/libraries/{test_library_id}", headers=auth)
    assert r_del.status_code == 204, (r_del.status_code, r_del.text)

    # Check that both assets have deleted_at set in the DB
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            for asset_id in (asset_id_1, asset_id_2):
                row = conn.execute(
                    text("SELECT deleted_at FROM assets WHERE asset_id = :asset_id"),
                    {"asset_id": asset_id},
                ).fetchone()
                assert row is not None, f"Asset {asset_id} not found"
                assert row[0] is not None, (
                    f"Asset {asset_id} should have deleted_at set after library trash"
                )
    finally:
        engine.dispose()

    # Assets should no longer appear in active assets via API
    r_list = client.get(
        "/v1/assets/page",
        params={"library_id": test_library_id},
        headers=auth,
    )
    assert r_list.json().get("items", []) == [], "Trashed library assets should not appear in active list"



# ---------------------------------------------------------------------------
# FIX-12 (BUG-8): Scan update clears stale artifact keys
# ---------------------------------------------------------------------------



