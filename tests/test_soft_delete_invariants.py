"""Defensive integration tests for soft-delete / active_assets view invariants.

These tests exist to catch the recurring class of bug where code queries the
`assets` table directly instead of going through the `active_assets` view
(which filters deleted_at IS NULL). They run against a real Postgres instance
so they catch SQL-level regressions that mocks cannot.

Every test here corresponds to a boundary that has burned us before:
- Trashed assets must not appear in list/page/get endpoints
- Trashed assets must be restored (not duplicated or silently updated) when re-scanned
- Bulk scan upsert (ON CONFLICT DO UPDATE) must clear deleted_at on restore
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.cli.scanner import scan_library
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def sd_client() -> tuple[TestClient, str]:
    """Module-scoped testcontainer fixture identical to scanner_client."""
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
                    json={"name": "SoftDeleteTestTenant", "plan": "free"},
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
                yield client, api_key
        _engines.clear()


def _lib(client: TestClient, api_key: str, root_path: str) -> dict:
    r = client.post(
        "/v1/libraries",
        json={"name": f"SDLib_{secrets.token_urlsafe(4)}", "root_path": root_path},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _scan(client: TestClient, api_key: str, library: dict) -> None:
    auth = _AuthClient(client, api_key)
    result = scan_library(auth, library, force=True)
    assert result.status == "complete", result


def _trash(client: TestClient, api_key: str, asset_id: str) -> None:
    r = client.delete(
        f"/v1/assets/{asset_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 204, r.text


def _page_all(client: TestClient, api_key: str, library_id: str) -> list[dict]:
    """Collect all assets from the cursor-paginated /v1/assets/page endpoint."""
    assets = []
    cursor = None
    while True:
        params: dict = {"library_id": library_id, "limit": 500, "sort": "asset_id", "dir": "asc"}
        if cursor:
            params["after"] = cursor
        r = client.get(
            "/v1/assets/page",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            break
        assets.extend(items)
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return assets


# ---------------------------------------------------------------------------
# GET /v1/assets/page must never return trashed assets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_page_excludes_trashed_asset(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """Trashed asset must not appear in /v1/assets/page (active_assets view)."""
    client, api_key = sd_client
    (tmp_path / "keep.jpg").write_bytes(b"k")
    (tmp_path / "trash.jpg").write_bytes(b"t")
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    page = _page_all(client, api_key, library["library_id"])
    assert len(page) == 2
    trash_id = next(a["asset_id"] for a in page if a["rel_path"] == "trash.jpg")

    _trash(client, api_key, trash_id)

    page_after = _page_all(client, api_key, library["library_id"])
    rel_paths = [a["rel_path"] for a in page_after]
    assert "trash.jpg" not in rel_paths
    assert "keep.jpg" in rel_paths
    assert len(page_after) == 1


# ---------------------------------------------------------------------------
# GET /v1/assets/{asset_id} must return 404 for trashed assets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_get_by_id_returns_404_for_trashed(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """GET /v1/assets/{asset_id} must 404 after trash, not return the trashed record."""
    client, api_key = sd_client
    (tmp_path / "img.jpg").write_bytes(b"x")
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    page = _page_all(client, api_key, library["library_id"])
    assert len(page) == 1
    asset_id = page[0]["asset_id"]

    r = client.get(f"/v1/assets/{asset_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200

    _trash(client, api_key, asset_id)

    r = client.get(f"/v1/assets/{asset_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 404, f"Expected 404 for trashed asset, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# GET /v1/assets list must exclude trashed assets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_list_excludes_trashed_asset(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """GET /v1/assets?library_id=... must not include trashed assets."""
    client, api_key = sd_client
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "b.jpg").write_bytes(b"b")
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    r = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    assert len(r.json()) == 2

    trash_id = next(a["asset_id"] for a in r.json() if a["rel_path"] == "b.jpg")
    _trash(client, api_key, trash_id)

    r2 = client.get(
        "/v1/assets",
        params={"library_id": library["library_id"]},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r2.status_code == 200
    rel_paths = [a["rel_path"] for a in r2.json()]
    assert "b.jpg" not in rel_paths
    assert "a.jpg" in rel_paths


# ---------------------------------------------------------------------------
# Re-scanning a trashed file must restore it, not create a zombie
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_rescan_restores_trashed_asset(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """
    Scenario: scan → trash → re-scan the same file.

    Expected: the asset is restored (visible via active_assets again).
    The same asset_id should be reused (restore, not duplicate).
    No constraint violation from the unique index on (library_id, rel_path).
    The asset must NOT remain trashed after the second scan.
    """
    client, api_key = sd_client
    (tmp_path / "restore_me.jpg").write_bytes(b"original")
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    page1 = _page_all(client, api_key, library["library_id"])
    assert len(page1) == 1
    original_asset_id = page1[0]["asset_id"]

    _trash(client, api_key, original_asset_id)

    # After trash: asset must not appear in page
    assert _page_all(client, api_key, library["library_id"]) == []

    # Re-scan: file still exists on disk
    _scan(client, api_key, library)

    page2 = _page_all(client, api_key, library["library_id"])
    assert len(page2) == 1, f"Expected 1 active asset after restore-scan, got {len(page2)}"
    assert page2[0]["rel_path"] == "restore_me.jpg"

    # Must be the same asset_id (restored, not a new record)
    assert page2[0]["asset_id"] == original_asset_id, (
        f"Expected same asset_id {original_asset_id} after restore, "
        f"got new id {page2[0]['asset_id']}"
    )

    # GET by ID must now return 200
    r = client.get(
        f"/v1/assets/{original_asset_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, f"Restored asset not accessible by ID: {r.text}"


# ---------------------------------------------------------------------------
# Batch trash: all trashed assets disappear from page
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_batch_trash_excluded_from_page(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """DELETE /v1/assets (batch) must remove all specified assets from active_assets view."""
    client, api_key = sd_client
    for i in range(5):
        (tmp_path / f"batch_{i}.jpg").write_bytes(bytes([i]))
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    page = _page_all(client, api_key, library["library_id"])
    assert len(page) == 5
    all_ids = [a["asset_id"] for a in page]

    r = client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": all_ids},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["trashed"]) == 5
    assert body["not_found"] == []

    assert _page_all(client, api_key, library["library_id"]) == []


# ---------------------------------------------------------------------------
# Re-scanning after BATCH trash restores all files
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_rescan_restores_batch_trashed_assets(
    sd_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    """
    Bulk-trash N assets then re-scan: all must be restored via ON CONFLICT DO UPDATE
    clearing deleted_at. No constraint violations, no zombie records.
    """
    client, api_key = sd_client
    n = 3
    for i in range(n):
        (tmp_path / f"bulk_{i}.jpg").write_bytes(bytes([i]))
    library = _lib(client, api_key, str(tmp_path))
    _scan(client, api_key, library)

    page1 = _page_all(client, api_key, library["library_id"])
    assert len(page1) == n
    original_ids = {a["rel_path"]: a["asset_id"] for a in page1}

    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": list(original_ids.values())},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert _page_all(client, api_key, library["library_id"]) == []

    _scan(client, api_key, library)

    page2 = _page_all(client, api_key, library["library_id"])
    assert len(page2) == n, f"Expected {n} restored assets, got {len(page2)}"

    restored_ids = {a["rel_path"]: a["asset_id"] for a in page2}
    for rel_path, orig_id in original_ids.items():
        assert restored_ids[rel_path] == orig_id, (
            f"{rel_path}: expected restored id {orig_id}, got {restored_ids[rel_path]}"
        )
