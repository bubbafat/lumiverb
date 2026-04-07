"""Collections API integration tests. Uses testcontainers Postgres + tenant DB."""

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


def _ingest_asset(client, api_key, library_id, rel_path) -> str:
    """Helper: ingest a minimal asset, return asset_id."""
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


@pytest.fixture(scope="module")
def collections_env():
    """Two testcontainers Postgres: control + tenant. Yield (client, api_key, library_id)."""
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
        os.environ["ADMIN_KEY"] = "test-admin-collections"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "CollectionsTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-collections"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
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
                cr = client.post(
                    "/v1/libraries",
                    json={"name": "CollectionTestLib", "root_path": "/tmp/col-test"},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert cr.status_code == 200
                library_id = cr.json()["library_id"]
                yield client, api_key, library_id

        _engines.clear()


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_collection(collections_env):
    client, api_key, _ = collections_env
    r = client.post(
        "/v1/collections",
        json={"name": "My Collection"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Collection"
    assert data["collection_id"].startswith("col_")
    assert data["asset_count"] == 0
    assert data["sort_order"] == "manual"
    assert data["visibility"] == "private"
    assert data["ownership"] == "own"
    assert data["cover_asset_id"] is None


@pytest.mark.slow
def test_list_collections(collections_env):
    client, api_key, _ = collections_env
    r = client.get("/v1/collections", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1


@pytest.mark.slow
def test_get_collection(collections_env):
    client, api_key, _ = collections_env
    # Create one
    r = client.post(
        "/v1/collections",
        json={"name": "GetTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    assert r2.json()["name"] == "GetTest"


@pytest.mark.slow
def test_get_collection_404(collections_env):
    client, api_key, _ = collections_env
    r = client.get("/v1/collections/col_nonexistent", headers=_headers(api_key))
    assert r.status_code == 404


@pytest.mark.slow
def test_update_collection(collections_env):
    client, api_key, _ = collections_env
    r = client.post(
        "/v1/collections",
        json={"name": "UpdateTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"name": "Updated Name", "description": "A description", "sort_order": "added_at"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "A description"
    assert data["sort_order"] == "added_at"


@pytest.mark.slow
def test_update_collection_invalid_sort(collections_env):
    client, api_key, _ = collections_env
    r = client.post(
        "/v1/collections",
        json={"name": "BadSort"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"sort_order": "invalid"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400


@pytest.mark.slow
def test_delete_collection(collections_env):
    client, api_key, _ = collections_env
    r = client.post(
        "/v1/collections",
        json={"name": "DeleteMe"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.request("DELETE", f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 204

    r3 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r3.status_code == 404


@pytest.mark.slow
def test_delete_collection_404(collections_env):
    client, api_key, _ = collections_env
    r = client.request("DELETE", "/v1/collections/col_nonexistent", headers=_headers(api_key))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Batch add / remove
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_add_and_list_assets(collections_env):
    client, api_key, library_id = collections_env
    # Create assets
    a1 = _ingest_asset(client, api_key, library_id, "col/photo1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/photo2.jpg")

    # Create collection
    r = client.post(
        "/v1/collections",
        json={"name": "AssetTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Add assets
    r2 = client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["added"] == 2

    # List assets
    r3 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r3.status_code == 200
    items = r3.json()["items"]
    assert len(items) == 2

    # Asset count in collection detail
    r4 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r4.json()["asset_count"] == 2


@pytest.mark.slow
def test_idempotent_add(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/idempotent1.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "IdempotentTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Add once
    client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    # Add again — should be idempotent
    r2 = client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.json()["added"] == 0

    # Still just one asset
    r3 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


@pytest.mark.slow
def test_add_trashed_asset_rejected(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/trashable.jpg")

    # Trash the asset
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )

    r = client.post(
        "/v1/collections",
        json={"name": "TrashTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Try to add trashed asset
    r2 = client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 404


@pytest.mark.slow
def test_remove_assets(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/removable1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/removable2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "RemoveTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )

    # Remove one
    r2 = client.request(
        "DELETE",
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["removed"] == 1

    # Only one left
    r3 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Create with asset_ids (atomic create+populate)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_with_assets(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/atomic1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/atomic2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "Atomic Create", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["asset_count"] == 2


# ---------------------------------------------------------------------------
# Soft-delete visibility
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_trashed_asset_hidden_in_collection(collections_env):
    """Trashing an asset hides it from collection views but row persists."""
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/soft1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/soft2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "SoftDeleteTest", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]
    assert r.json()["asset_count"] == 2

    # Trash one asset
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )

    # Count should drop
    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 1

    # Asset list should only show the non-trashed one
    r3 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert len(r3.json()["items"]) == 1
    assert r3.json()["items"][0]["asset_id"] == a2


@pytest.mark.slow
def test_restore_asset_restores_collection_membership(collections_env):
    """Restoring a trashed asset brings it back into the collection."""
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/restore1.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "RestoreTest", "asset_ids": [a1]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Trash
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 0

    # Restore
    client.post(
        f"/v1/assets/{a1}/restore",
        headers=_headers(api_key),
    )
    r3 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Cover image resolution
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_cover_defaults_to_first_asset(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/cover1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/cover2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "CoverTest", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]
    # First by position = a1
    assert r.json()["cover_asset_id"] == a1


@pytest.mark.slow
def test_cover_explicit_set(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/coverex1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/coverex2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "CoverExplicit", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Set explicit cover
    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"cover_asset_id": a2},
        headers=_headers(api_key),
    )
    assert r2.json()["cover_asset_id"] == a2


@pytest.mark.slow
def test_cover_stale_self_heals(collections_env):
    """Cover falls back to first-by-position when cover asset is trashed."""
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/coverheal1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/coverheal2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "CoverHeal", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Set a1 as cover
    client.patch(
        f"/v1/collections/{col_id}",
        json={"cover_asset_id": a1},
        headers=_headers(api_key),
    )

    # Trash a1
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )

    # Cover should fall back to a2 (first active by position)
    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.json()["cover_asset_id"] == a2


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reorder(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/reorder1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/reorder2.jpg")
    a3 = _ingest_asset(client, api_key, library_id, "col/reorder3.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "ReorderTest", "asset_ids": [a1, a2, a3]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Reorder: reverse
    r2 = client.patch(
        f"/v1/collections/{col_id}/reorder",
        json={"asset_ids": [a3, a2, a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200

    # Verify order
    r3 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    ids = [item["asset_id"] for item in r3.json()["items"]]
    assert ids == [a3, a2, a1]


@pytest.mark.slow
def test_reorder_partial_rejected(collections_env):
    """Partial reorder (missing assets) is rejected with 400."""
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/partial1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/partial2.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "PartialReorder", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Only provide one of two
    r2 = client.patch(
        f"/v1/collections/{col_id}/reorder",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Delete collection does not affect source assets
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_delete_collection_preserves_assets(collections_env):
    client, api_key, library_id = collections_env
    a1 = _ingest_asset(client, api_key, library_id, "col/preserved.jpg")

    r = client.post(
        "/v1/collections",
        json={"name": "DeletePreserve", "asset_ids": [a1]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Delete collection
    client.request("DELETE", f"/v1/collections/{col_id}", headers=_headers(api_key))

    # Asset should still exist
    r2 = client.get(
        f"/v1/assets/page?library_id={library_id}",
        headers=_headers(api_key),
    )
    asset_ids = [item["asset_id"] for item in r2.json()["items"]]
    assert a1 in asset_ids
