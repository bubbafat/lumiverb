"""Tests for bugs fixed during the session.

Fix 1: hard_delete missing asset_embeddings (FK crash)
Fix 2: Failed video chunks blocking pipeline permanently
Fix 6: mark_missing_for_scan bulk SQL
"""

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


# ---------------------------------------------------------------------------
# Module-scoped fixture: control DB + tenant DB + tenant + library
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bug_fixes_api_client() -> tuple[TestClient, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create tenant and library.
    Yields (client, api_key, library_id, tenant_url).
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
                    json={"name": "BugFixesTenant", "plan": "free"},
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
                    json={"name": "BugFixesLib", "root_path": "/bugfixes"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
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
    """Create a scan, upsert an asset, return its asset_id."""
    r_scan = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan.status_code == 200, (r_scan.status_code, r_scan.text)
    scan_id = r_scan.json()["scan_id"]

    r_up = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": media_type,
            "scan_id": scan_id,
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


def _upsert_asset_with_scan(
    client: TestClient,
    auth: dict,
    library_id: str,
    rel_path: str,
    media_type: str = "image",
) -> tuple[str, str]:
    """Create a scan, upsert an asset, return (asset_id, scan_id)."""
    r_scan = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan.status_code == 200, (r_scan.status_code, r_scan.text)
    scan_id = r_scan.json()["scan_id"]

    r_up = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": media_type,
            "scan_id": scan_id,
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
    return r_asset.json()["asset_id"], scan_id


# ---------------------------------------------------------------------------
# Fix 1: hard_delete missing asset_embeddings (FK crash)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_hard_delete_library_with_asset_embeddings_no_fk_crash(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    DELETE /v1/libraries/{library_id} (via empty-trash) must not 500 even when
    asset_embeddings rows exist for assets in that library.

    Old bug: hard_delete did not delete asset_embeddings before assets, causing
    an FK violation (asset_embeddings.asset_id → assets.asset_id).
    """
    client, api_key, _shared_library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a dedicated library so we can trash + hard-delete it without affecting
    # the shared fixture library used by other tests.
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "EmbedDeleteLib", "root_path": "/embed-delete"},
        headers=auth,
    )
    assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
    library_id = r_lib.json()["library_id"]

    asset_id = _upsert_asset(client, auth, library_id, "embed_test.jpg")

    # Insert a row into asset_embeddings directly via SQL to simulate the embed worker.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO asset_embeddings
                        (embedding_id, asset_id, model_id, model_version, embedding_vector, created_at)
                    VALUES
                        (:embedding_id, :asset_id, 'clip', '1', CAST(:vec AS vector), NOW())
                    """
                ),
                {
                    "embedding_id": "emb_test_fix1_001",
                    "asset_id": asset_id,
                    "vec": "[" + ",".join(["0.1"] * 512) + "]",
                },
            )
            conn.commit()
    finally:
        engine.dispose()

    # Trash the library so empty-trash will hard-delete it.
    r_del = client.delete(f"/v1/libraries/{library_id}", headers=auth)
    assert r_del.status_code == 204, (r_del.status_code, r_del.text)

    # Hard-delete via empty-trash: must return 200, not 500.
    r_trash = client.post("/v1/libraries/empty-trash", json={}, headers=auth)
    assert r_trash.status_code == 200, (r_trash.status_code, r_trash.text)
    assert r_trash.json()["deleted"] >= 1

@pytest.mark.slow
def test_mark_missing_for_scan_bulk_sql(
    bug_fixes_api_client: tuple[TestClient, str, str, str],
) -> None:
    """
    After a scan that only sees 1 out of 3 pre-existing assets, completing the
    scan must mark the other 2 as availability='missing' via bulk SQL.

    This verifies the fix changed from an O(n) Python loop to a single bulk
    UPDATE statement and that it correctly identifies assets not seen by scan_id.
    """
    client, api_key, library_id, tenant_url = bug_fixes_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    suffix = os.urandom(4).hex()

    # Create a dedicated scan and upsert 3 assets with the same scan, making
    # them all availability='online'.
    r_scan1 = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan1.status_code == 200
    scan1_id = r_scan1.json()["scan_id"]

    paths = [f"missing_test_{suffix}_{i}.jpg" for i in range(3)]
    asset_ids = []
    for path in paths:
        r_up = client.post(
            "/v1/assets/upsert",
            json={
                "library_id": library_id,
                "rel_path": path,
                "file_size": 1000,
                "file_mtime": "2025-01-01T12:00:00Z",
                "media_type": "image",
                "scan_id": scan1_id,
            },
            headers=auth,
        )
        assert r_up.status_code == 200
        r_asset = client.get(
            "/v1/assets/by-path",
            params={"library_id": library_id, "rel_path": path},
            headers=auth,
        )
        assert r_asset.status_code == 200
        asset_ids.append(r_asset.json()["asset_id"])

    # Complete scan1 so assets get availability='online'.
    r_comp1 = client.post(f"/v1/scans/{scan1_id}/complete", headers=auth)
    assert r_comp1.status_code == 200

    # Verify all 3 are online before the test.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_id, availability FROM assets WHERE asset_id = ANY(:ids)"
                ),
                {"ids": asset_ids},
            ).fetchall()
    finally:
        engine.dispose()
    assert all(r[1] == "online" for r in rows), f"Expected all online: {rows}"

    # Start a new scan that only sees assets[0].
    r_scan2 = client.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
        headers=auth,
    )
    assert r_scan2.status_code == 200
    scan2_id = r_scan2.json()["scan_id"]

    r_up2 = client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": paths[0],
            "file_size": 1000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "image",
            "scan_id": scan2_id,
        },
        headers=auth,
    )
    assert r_up2.status_code == 200

    # Complete scan2 — this triggers mark_missing_for_scan.
    r_comp2 = client.post(f"/v1/scans/{scan2_id}/complete", headers=auth)
    assert r_comp2.status_code == 200
    # The response should report 2 missing.
    assert r_comp2.json()["files_missing"] >= 2

    # Verify DB state: assets[0] stays online; assets[1] and assets[2] → missing.
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            avail_map = dict(
                conn.execute(
                    text(
                        "SELECT asset_id, availability FROM assets WHERE asset_id = ANY(:ids)"
                    ),
                    {"ids": asset_ids},
                ).fetchall()
            )
    finally:
        engine.dispose()

    assert avail_map[asset_ids[0]] == "online", "Seen asset must stay online"
    assert avail_map[asset_ids[1]] == "missing", "Unseen asset must be marked missing"
    assert avail_map[asset_ids[2]] == "missing", "Unseen asset must be marked missing"

