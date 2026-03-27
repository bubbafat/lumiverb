"""Tests for audit fixes implemented during the audit session.

FIX-1 (BUG-1): Stale proxy/thumbnail recovery re-enqueues correct job type
FIX-2 (BUG-2): AssetResponse includes duration_sec
FIX-3 (BUG-3): last_scan_error cleared on success
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

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


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
                    json={"name": "AuditFixesTenant", "plan": "free"},
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


def _count_pending_jobs(tenant_url: str, asset_id: str, job_type: str) -> int:
    """Count pending/claimed jobs of the given type for the given asset."""
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*)::int FROM worker_jobs "
                    "WHERE asset_id = :asset_id AND job_type = :job_type "
                    "AND status IN ('pending', 'claimed')"
                ),
                {"asset_id": asset_id, "job_type": job_type},
            ).fetchone()
            return int(row[0]) if row else 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# FIX-1 (BUG-1): Stale proxy/thumbnail recovery re-enqueues correct job type
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_stale_proxy_reenqueues_proxy_job(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /assets/{id}/proxy for a stale proxy_key returns 202 and enqueues a proxy job."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "stale_proxy_test.jpg")
    # Set a nonexistent proxy_key
    _set_proxy_key(tenant_url, asset_id, "nonexistent/fake_proxy.jpg")

    r = client.get(f"/v1/assets/{asset_id}/proxy", headers=auth)
    assert r.status_code == 202, (r.status_code, r.text)
    assert r.json().get("status") == "generating"

    # A proxy job should now be pending
    count = _count_pending_jobs(tenant_url, asset_id, "proxy")
    assert count >= 1, f"Expected proxy job to be enqueued, got {count}"


@pytest.mark.slow
def test_stale_image_thumbnail_reenqueues_proxy_job(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /assets/{id}/thumbnail for a stale thumbnail_key on an image returns 202 and enqueues proxy job."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "stale_thumb_image.jpg", "image")
    _set_thumbnail_key(tenant_url, asset_id, "nonexistent/fake_thumb.jpg")

    r = client.get(f"/v1/assets/{asset_id}/thumbnail", headers=auth)
    assert r.status_code == 202, (r.status_code, r.text)
    assert r.json().get("status") == "generating"

    # For images, a stale thumbnail should enqueue proxy (not video-index)
    proxy_count = _count_pending_jobs(tenant_url, asset_id, "proxy")
    assert proxy_count >= 1, f"Expected proxy job enqueued for image stale thumbnail, got {proxy_count}"

    video_index_count = _count_pending_jobs(tenant_url, asset_id, "video-index")
    assert video_index_count == 0, f"Should NOT enqueue video-index for image thumbnail, got {video_index_count}"


@pytest.mark.slow
def test_stale_video_thumbnail_reenqueues_video_index_job(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """GET /assets/{id}/thumbnail for a stale thumbnail_key on a video returns 202 and enqueues video-index."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "stale_thumb_video.mp4", "video")
    _set_thumbnail_key(tenant_url, asset_id, "nonexistent/fake_video_thumb.jpg")

    r = client.get(f"/v1/assets/{asset_id}/thumbnail", headers=auth)
    assert r.status_code == 202, (r.status_code, r.text)
    assert r.json().get("status") == "generating"

    video_index_count = _count_pending_jobs(tenant_url, asset_id, "video-index")
    assert video_index_count >= 1, f"Expected video-index job enqueued for video stale thumbnail, got {video_index_count}"


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
# FIX-3 (BUG-3): last_scan_error cleared on success
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_last_scan_error_cleared_on_success(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """update_scan_status('complete') clears last_scan_error set by a prior error status."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a dedicated library for this test
    r_lib = client.post(
        "/v1/libraries",
        json={"name": "ScanErrorLib", "root_path": "/scan-error"},
        headers=auth,
    )
    assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
    test_library_id = r_lib.json()["library_id"]

    from sqlmodel import Session as SQLModelSession
    from src.repository.tenant import LibraryRepository

    engine = create_engine(tenant_url)
    try:
        with SQLModelSession(engine) as session:
            lib_repo = LibraryRepository(session)
            # Set error status first
            lib_repo.update_scan_status(test_library_id, "error", "some error message")
            lib_check = lib_repo.get_by_id(test_library_id)
            assert lib_check is not None
            assert lib_check.last_scan_error == "some error message"

            # Now complete successfully — error should be cleared
            lib_repo.update_scan_status(test_library_id, "complete")
            lib_check2 = lib_repo.get_by_id(test_library_id)
            assert lib_check2 is not None
            assert lib_check2.last_scan_error is None, (
                f"Expected last_scan_error=None after complete, got {lib_check2.last_scan_error!r}"
            )
    finally:
        engine.dispose()


@pytest.mark.slow
def test_last_scan_error_preserved_when_error_message_is_none(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """update_scan_status('error', error=None) must not clear a previously set last_scan_error."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_lib = client.post(
        "/v1/libraries",
        json={"name": "ScanErrorPreserveLib", "root_path": "/scan-error-preserve"},
        headers=auth,
    )
    assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
    test_library_id = r_lib.json()["library_id"]

    from sqlmodel import Session as SQLModelSession
    from src.repository.tenant import LibraryRepository

    engine = create_engine(tenant_url)
    try:
        with SQLModelSession(engine) as session:
            lib_repo = LibraryRepository(session)
            lib_repo.update_scan_status(test_library_id, "error", "original error")
            lib_check = lib_repo.get_by_id(test_library_id)
            assert lib_check is not None
            assert lib_check.last_scan_error == "original error"

            # Calling error with no message must NOT wipe the previous error
            lib_repo.update_scan_status(test_library_id, "error", error=None)
            lib_check2 = lib_repo.get_by_id(test_library_id)
            assert lib_check2 is not None
            assert lib_check2.last_scan_error == "original error", (
                f"Expected last_scan_error preserved, got {lib_check2.last_scan_error!r}"
            )
    finally:
        engine.dispose()


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
        "/v1/assets",
        params={"library_id": test_library_id},
        headers=auth,
    )
    assert r_list.status_code == 200
    assert r_list.json() == [], "Trashed library assets should not appear in active list"



# ---------------------------------------------------------------------------
# FIX-12 (BUG-8): Scan update clears stale artifact keys
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_scan_update_clears_stale_proxy_keys(
    audit_fixes_client: tuple[TestClient, str, str, str],
) -> None:
    """update_for_scan with status=pending clears proxy_key and thumbnail_key."""
    client, api_key, library_id, tenant_url = audit_fixes_client
    auth = {"Authorization": f"Bearer {api_key}"}

    asset_id = _upsert_asset(client, auth, library_id, "scan_clear_proxy.jpg")

    # Set proxy_key and thumbnail_key directly
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE assets SET proxy_key = 'old_proxy', thumbnail_key = 'old_thumb' "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            conn.commit()
    finally:
        engine.dispose()

    # Call update_for_scan directly via the repo
    from datetime import datetime, timezone
    from sqlmodel import Session as SQLModelSession
    from src.repository.tenant import AssetRepository
    from src.core import asset_status

    # Fetch the real last_scan_id so we don't violate the FK constraint
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_scan_id FROM assets WHERE asset_id = :aid"),
            {"aid": asset_id},
        ).fetchone()
        real_scan_id = row[0]
    engine.dispose()

    engine = create_engine(tenant_url)
    try:
        with SQLModelSession(engine) as session:
            asset_repo = AssetRepository(session)
            # Simulate file changed → reset to pending
            asset_repo.update_for_scan(
                asset_id=asset_id,
                file_size=9999,
                file_mtime=datetime(2025, 6, 1, tzinfo=timezone.utc),
                availability="online",
                status=asset_status.PENDING,
                last_scan_id=real_scan_id,
            )
            updated = asset_repo.get_by_id(asset_id)
            assert updated is not None
            assert updated.proxy_key is None, (
                f"Expected proxy_key=None after update_for_scan(pending), got {updated.proxy_key!r}"
            )
            assert updated.thumbnail_key is None, (
                f"Expected thumbnail_key=None after update_for_scan(pending), got {updated.thumbnail_key!r}"
            )
    finally:
        engine.dispose()


