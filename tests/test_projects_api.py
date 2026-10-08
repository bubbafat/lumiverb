"""Projects API integration tests. Uses testcontainers Postgres + tenant DB."""

from __future__ import annotations

import io
import json
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
def projects_env():
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
        os.environ["ADMIN_KEY"] = "test-admin-projects"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "ProjectsTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-projects"},
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
                    json={"name": "ProjectTestLib", "root_path": "/tmp/col-test"},
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
def test_create_project(projects_env):
    client, api_key, _ = projects_env
    r = client.post(
        "/v1/projects",
        json={"name": "My Project"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Project"
    assert data["project_id"].startswith("prj_")
    assert data["asset_count"] == 0
    assert data["sort_order"] == "manual"
    assert data["visibility"] == "private"
    assert data["ownership"] == "own"
    assert data["cover_asset_id"] is None


@pytest.mark.slow
def test_list_projects(projects_env):
    client, api_key, _ = projects_env
    r = client.get("/v1/projects", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1


@pytest.mark.slow
def test_get_project(projects_env):
    client, api_key, _ = projects_env
    # Create one
    r = client.post(
        "/v1/projects",
        json={"name": "GetTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    assert r2.json()["name"] == "GetTest"


@pytest.mark.slow
def test_get_project_404(projects_env):
    client, api_key, _ = projects_env
    r = client.get("/v1/projects/col_nonexistent", headers=_headers(api_key))
    assert r.status_code == 404


@pytest.mark.slow
def test_update_project(projects_env):
    client, api_key, _ = projects_env
    r = client.post(
        "/v1/projects",
        json={"name": "UpdateTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.patch(
        f"/v1/projects/{col_id}",
        json={"name": "Updated Name", "description": "A description", "sort_order": "added_at"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    data = r2.json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "A description"
    assert data["sort_order"] == "added_at"


@pytest.mark.slow
def test_update_project_invalid_sort(projects_env):
    client, api_key, _ = projects_env
    r = client.post(
        "/v1/projects",
        json={"name": "BadSort"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.patch(
        f"/v1/projects/{col_id}",
        json={"sort_order": "invalid"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400


@pytest.mark.slow
def test_delete_project(projects_env):
    client, api_key, _ = projects_env
    r = client.post(
        "/v1/projects",
        json={"name": "DeleteMe"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.request("DELETE", f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 204

    r3 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r3.status_code == 404


@pytest.mark.slow
def test_delete_project_404(projects_env):
    client, api_key, _ = projects_env
    r = client.request("DELETE", "/v1/projects/col_nonexistent", headers=_headers(api_key))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Batch add / remove
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_add_and_list_assets(projects_env):
    client, api_key, library_id = projects_env
    # Create assets
    a1 = _ingest_asset(client, api_key, library_id, "col/photo1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/photo2.jpg")

    # Create project
    r = client.post(
        "/v1/projects",
        json={"name": "AssetTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Add assets
    r2 = client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["added"] == 2

    # List assets
    r3 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r3.status_code == 200
    items = r3.json()["items"]
    assert len(items) == 2

    # Asset count in project detail
    r4 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r4.json()["asset_count"] == 2


@pytest.mark.slow
def test_idempotent_add(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/idempotent1.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "IdempotentTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Add once
    client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    # Add again — should be idempotent
    r2 = client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.json()["added"] == 0

    # Still just one asset
    r3 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


@pytest.mark.slow
def test_add_trashed_asset_rejected(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/trashable.jpg")

    # Trash the asset
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )

    r = client.post(
        "/v1/projects",
        json={"name": "TrashTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Try to add trashed asset
    r2 = client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 404


@pytest.mark.slow
def test_remove_assets(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/removable1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/removable2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "RemoveTest"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )

    # Remove one
    r2 = client.request(
        "DELETE",
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["removed"] == 1

    # Only one left
    r3 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Create with asset_ids (atomic create+populate)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_with_assets(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/atomic1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/atomic2.jpg")

    r = client.post(
        "/v1/projects",
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
def test_trashed_asset_hidden_in_project(projects_env):
    """Trashing an asset hides it from project views but row persists."""
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/soft1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/soft2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "SoftDeleteTest", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]
    assert r.json()["asset_count"] == 2

    # Trash one asset
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )

    # Count should drop
    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 1

    # Asset list should only show the non-trashed one
    r3 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert len(r3.json()["items"]) == 1
    assert r3.json()["items"][0]["asset_id"] == a2


@pytest.mark.slow
def test_restore_asset_restores_project_membership(projects_env):
    """Restoring a trashed asset brings it back into the project."""
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/restore1.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "RestoreTest", "asset_ids": [a1]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Trash
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 0

    # Restore
    client.post(
        f"/v1/assets/{a1}/restore",
        headers=_headers(api_key),
    )
    r3 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Cover image resolution
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_cover_defaults_to_first_asset(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/cover1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/cover2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "CoverTest", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]
    # First by position = a1
    assert r.json()["cover_asset_id"] == a1


@pytest.mark.slow
def test_cover_explicit_set(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/coverex1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/coverex2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "CoverExplicit", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Set explicit cover
    r2 = client.patch(
        f"/v1/projects/{col_id}",
        json={"cover_asset_id": a2},
        headers=_headers(api_key),
    )
    assert r2.json()["cover_asset_id"] == a2


@pytest.mark.slow
def test_cover_stale_self_heals(projects_env):
    """Cover falls back to first-by-position when cover asset is trashed."""
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/coverheal1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/coverheal2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "CoverHeal", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Set a1 as cover
    client.patch(
        f"/v1/projects/{col_id}",
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
    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.json()["cover_asset_id"] == a2


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_reorder(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/reorder1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/reorder2.jpg")
    a3 = _ingest_asset(client, api_key, library_id, "col/reorder3.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "ReorderTest", "asset_ids": [a1, a2, a3]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Reorder: reverse
    r2 = client.patch(
        f"/v1/projects/{col_id}/reorder",
        json={"asset_ids": [a3, a2, a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200

    # Verify order
    r3 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    ids = [item["asset_id"] for item in r3.json()["items"]]
    assert ids == [a3, a2, a1]


@pytest.mark.slow
def test_reorder_partial_rejected(projects_env):
    """Partial reorder (missing assets) is rejected with 400."""
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/partial1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "col/partial2.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "PartialReorder", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Only provide one of two
    r2 = client.patch(
        f"/v1/projects/{col_id}/reorder",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Delete project does not affect source assets
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_delete_project_preserves_assets(projects_env):
    client, api_key, library_id = projects_env
    a1 = _ingest_asset(client, api_key, library_id, "col/preserved.jpg")

    r = client.post(
        "/v1/projects",
        json={"name": "DeletePreserve", "asset_ids": [a1]},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    # Delete project
    client.request("DELETE", f"/v1/projects/{col_id}", headers=_headers(api_key))

    # Asset should still exist
    r2 = client.get(
        f"/v1/assets/page?library_id={library_id}",
        headers=_headers(api_key),
    )
    asset_ids = [item["asset_id"] for item in r2.json()["items"]]
    assert a1 in asset_ids


# ---------------------------------------------------------------------------
# Pre-rename paths: macOS/iOS builds and share links still use /collections
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_legacy_collections_path_serves_projects(projects_env):
    client, api_key, _ = projects_env
    r = client.post("/v1/collections", json={"name": "Legacy client"}, headers=_headers(api_key))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["collection_id"] == created["project_id"]

    listed = client.get("/v1/projects", headers=_headers(api_key)).json()["items"]
    assert created["project_id"] in {p["project_id"] for p in listed}
    legacy = client.get(f"/v1/collections/{created['project_id']}", headers=_headers(api_key))
    assert legacy.status_code == 200
    assert legacy.json()["collection_id"] == created["project_id"]


@pytest.mark.slow
@pytest.mark.parametrize("prefix", ["/v1/public/projects", "/v1/public/collections"])
def test_public_project_readable_without_auth(projects_env, prefix):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, f"public/{prefix.rsplit('/', 1)[1]}.jpg")
    r = client.post(
        "/v1/projects",
        json={"name": "Shared reel", "asset_ids": [asset_id]},
        headers=_headers(api_key),
    )
    project_id = r.json()["project_id"]
    r = client.patch(
        f"/v1/projects/{project_id}", json={"visibility": "public"}, headers=_headers(api_key)
    )
    assert r.status_code == 200, r.text

    detail = client.get(f"{prefix}/{project_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == project_id
    assert detail.json()["collection_id"] == project_id
    assets = client.get(f"{prefix}/{project_id}/assets")
    assert assets.status_code == 200, assets.text
    assert [a["asset_id"] for a in assets.json()["items"]] == [asset_id]


# ---------------------------------------------------------------------------
# Lifecycle: active -> archived -> active
# ---------------------------------------------------------------------------


def _ids(client, api_key, **params) -> set[str]:
    r = client.get("/v1/projects", params=params, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return {p["project_id"] for p in r.json()["items"]}


@pytest.mark.slow
def test_archive_hides_project_but_keeps_its_clips(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "lifecycle/clip.jpg")
    project_id = client.post(
        "/v1/projects", json={"name": "Customer Video 123", "asset_ids": [asset_id]},
        headers=_headers(api_key),
    ).json()["project_id"]
    assert client.get(f"/v1/projects/{project_id}", headers=_headers(api_key)).json()["status"] == "active"

    r = client.patch(
        f"/v1/projects/{project_id}", json={"status": "archived"}, headers=_headers(api_key)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "archived"
    assert r.json()["archived_at"] is not None

    assert project_id not in _ids(client, api_key)
    assert project_id in _ids(client, api_key, status="archived")
    assert project_id in _ids(client, api_key, status="all")
    assert project_id not in _ids(client, api_key, status="active")
    detail = client.get(f"/v1/projects/{project_id}", headers=_headers(api_key))
    assert detail.status_code == 200
    assert detail.json()["asset_count"] == 1
    assets = client.get(f"/v1/projects/{project_id}/assets", headers=_headers(api_key))
    assert [a["asset_id"] for a in assets.json()["items"]] == [asset_id]

    r = client.patch(
        f"/v1/projects/{project_id}", json={"status": "active"}, headers=_headers(api_key)
    )
    assert r.json()["status"] == "active"
    assert r.json()["archived_at"] is None
    assert project_id in _ids(client, api_key)


@pytest.mark.slow
def test_invalid_status_rejected(projects_env):
    client, api_key, _ = projects_env
    project_id = client.post(
        "/v1/projects", json={"name": "Bad status"}, headers=_headers(api_key)
    ).json()["project_id"]

    r = client.patch(
        f"/v1/projects/{project_id}", json={"status": "deleted"}, headers=_headers(api_key)
    )
    assert r.status_code in (400, 422)
    r = client.get("/v1/projects", params={"status": "nope"}, headers=_headers(api_key))
    assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# Paging: every clip is reachable (the export depends on it)
# ---------------------------------------------------------------------------


def _ingest_taken(client, api_key, library_id, rel_path, taken_at) -> str:
    """Ingest an image with an EXIF capture time (None = no capture time)."""
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000",
            "media_type": "image"}
    if taken_at:
        data["exif"] = json.dumps({"taken_at": taken_at})
    r = client.post(
        "/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")},
        headers=_headers(api_key),
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _all_pages(client, api_key, project_id, limit) -> list[str]:
    ids: list[str] = []
    cursor = None
    for _ in range(50):
        params = {"limit": limit}
        if cursor:
            params["after"] = cursor
        r = client.get(f"/v1/projects/{project_id}/assets", params=params, headers=_headers(api_key))
        assert r.status_code == 200, r.text
        ids += [a["asset_id"] for a in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if not cursor:
            return ids
    raise AssertionError("pagination did not end")


@pytest.mark.slow
def test_static_project_pages_by_capture_time(projects_env):
    client, api_key, library_id = projects_env
    late = _ingest_taken(client, api_key, library_id, "paging/late.jpg", "2024-06-01T10:00:00+00:00")
    early = _ingest_taken(client, api_key, library_id, "paging/early.jpg", "2023-01-01T10:00:00+00:00")
    undated = _ingest_taken(client, api_key, library_id, "paging/undated.jpg", None)
    project_id = client.post(
        "/v1/projects",
        json={"name": "By capture time", "sort_order": "taken_at", "asset_ids": [late, undated, early]},
        headers=_headers(api_key),
    ).json()["project_id"]

    ids = _all_pages(client, api_key, project_id, limit=1)

    assert sorted(ids) == sorted([late, early, undated])
    assert ids.index(early) < ids.index(late)


@pytest.mark.slow
def test_smart_project_pages_past_one_page(projects_env):
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": "SmartPaging", "root_path": "/smart-paging"},
        headers=_headers(api_key),
    ).json()["library_id"]
    expected = {_ingest_asset(client, api_key, lib, f"s/{i}.jpg") for i in range(5)}
    project_id = client.post(
        "/v1/projects",
        json={"name": "Smart paging", "type": "smart",
              "saved_query": {"filters": [{"type": "library", "value": lib}]}},
        headers=_headers(api_key),
    ).json()["project_id"]

    ids = _all_pages(client, api_key, project_id, limit=2)

    assert len(ids) == len(set(ids)) == 5
    assert set(ids) == expected


@pytest.mark.slow
def test_shared_smart_project_uses_owner_ratings(projects_env):
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": "OwnerRatings", "root_path": "/owner-ratings"},
        headers=_headers(api_key),
    ).json()["library_id"]
    favorite = _ingest_asset(client, api_key, lib, "r/fav.jpg")
    _ingest_asset(client, api_key, lib, "r/other.jpg")
    client.put(f"/v1/assets/{favorite}/rating", json={"favorite": True}, headers=_headers(api_key))
    project_id = client.post(
        "/v1/projects",
        json={"name": "Owner favorites", "type": "smart", "visibility": "shared",
              "saved_query": {"filters": [{"type": "library", "value": lib},
                                          {"type": "favorite", "value": "yes"}]}},
        headers=_headers(api_key),
    ).json()["project_id"]
    viewer_key = client.post(
        "/v1/keys", json={"label": "viewer", "role": "editor"}, headers=_headers(api_key)
    ).json()["plaintext"]

    as_owner = _all_pages(client, api_key, project_id, limit=50)
    as_viewer = _all_pages(client, viewer_key, project_id, limit=50)

    assert as_owner == as_viewer == [favorite]


@pytest.mark.slow
@pytest.mark.parametrize("direction", ["desc", "asc"])
def test_paging_with_undated_clips_returns_each_clip_once(projects_env, direction):
    """Clips without a capture time sort last; paging across the boundary
    between dated and undated clips must neither drop nor repeat any."""
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": f"Mixed-{direction}", "root_path": f"/mixed-{direction}"},
        headers=_headers(api_key),
    ).json()["library_id"]
    dated = [
        _ingest_taken(client, api_key, lib, f"m/d{i}.jpg", f"2024-0{i + 1}-01T10:00:00+00:00")
        for i in range(3)
    ]
    undated = [_ingest_taken(client, api_key, lib, f"m/u{i}.jpg", None) for i in range(3)]
    expected = sorted(dated + undated)

    def pages(path: str, params: dict) -> list[str]:
        ids, cursor = [], None
        for _ in range(20):
            p = dict(params, limit=2)
            if cursor:
                p["after"] = cursor
            r = client.get(path, params=p, headers=_headers(api_key))
            assert r.status_code == 200, r.text
            ids += [a["asset_id"] for a in r.json()["items"]]
            cursor = r.json()["next_cursor"]
            if not cursor:
                return ids
        raise AssertionError("pagination did not end")

    # Smart project (the export path)
    project_id = client.post(
        "/v1/projects",
        json={"name": f"Mixed {direction}", "type": "smart",
              "saved_query": {"filters": [{"type": "library", "value": lib}],
                              "sort": "taken_at", "direction": direction}},
        headers=_headers(api_key),
    ).json()["project_id"]
    smart = _all_pages(client, api_key, project_id, limit=2)
    assert sorted(smart) == expected
    # Unified query and the asset page endpoint share the cursor logic
    query = pages("/v1/query", {"f": f"library:{lib}", "sort": "taken_at", "dir": direction})
    assert sorted(query) == expected
    page = pages("/v1/assets/page", {"library_id": lib, "sort": "taken_at", "dir": direction})
    assert sorted(page) == expected
    # Every advertised sort column pages too (exposure: all NULL here)
    exposure = pages("/v1/query", {"f": f"library:{lib}", "sort": "exposure_time_us", "dir": direction})
    assert sorted(exposure) == expected


@pytest.mark.slow
def test_a_project_created_public_has_a_working_link(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "public/at-create.jpg")
    r = client.post(
        "/v1/projects", json={"name": "Public from the start", "asset_ids": [asset_id], "visibility": "public"},
        headers=_headers(api_key),
    )
    project_id = r.json()["project_id"]
    assert client.get(f"/v1/public/projects/{project_id}").status_code == 200


# ---------------------------------------------------------------------------
# Trash: delete -> trash -> restore | delete forever (same pattern as assets)
# ---------------------------------------------------------------------------


def _project(client, api_key, name, asset_ids=(), **fields) -> str:
    r = client.post(
        "/v1/projects", json={"name": name, "asset_ids": list(asset_ids), **fields},
        headers=_headers(api_key),
    )
    assert r.status_code == 201, r.text
    return r.json()["project_id"]


def _trashed(client, api_key) -> dict[str, dict]:
    r = client.get("/v1/projects", params={"status": "trashed"}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return {p["project_id"]: p for p in r.json()["items"]}


def _other_user_key(client, api_key) -> str:
    r = client.post("/v1/keys", json={"label": "other", "role": "editor"}, headers=_headers(api_key))
    assert r.status_code in (200, 201), r.text
    return r.json()["plaintext"]


@pytest.mark.slow
def test_delete_moves_a_project_to_the_trash_with_its_clips(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "trash/clip.jpg")
    project_id = _project(client, api_key, "Customer Video 456", [asset_id])

    assert client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key)).status_code == 204

    assert client.get(f"/v1/projects/{project_id}", headers=_headers(api_key)).status_code == 404
    assert project_id not in _ids(client, api_key)
    assert project_id not in _ids(client, api_key, status="archived")
    assert project_id not in _ids(client, api_key, status="all")
    trashed = _trashed(client, api_key)
    assert trashed[project_id]["deleted_at"] is not None
    assert trashed[project_id]["asset_count"] == 1


@pytest.mark.slow
@pytest.mark.parametrize("archived", [False, True])
def test_restore_puts_a_project_back_where_it_was(projects_env, archived):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, f"trash/restore-{archived}.jpg")
    project_id = _project(client, api_key, f"Restore {archived}", [asset_id])
    if archived:
        client.patch(f"/v1/projects/{project_id}", json={"status": "archived"}, headers=_headers(api_key))
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))

    r = client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    assert r.status_code == 204, r.text

    item = client.get(f"/v1/projects/{project_id}", headers=_headers(api_key)).json()
    assert item["status"] == ("archived" if archived else "active")
    assert item["deleted_at"] is None
    assert project_id in _ids(client, api_key, status="archived" if archived else "active")
    assert project_id not in _trashed(client, api_key)
    assets = client.get(f"/v1/projects/{project_id}/assets", headers=_headers(api_key)).json()
    assert [a["asset_id"] for a in assets["items"]] == [asset_id]


@pytest.mark.slow
def test_restore_needs_a_project_in_the_trash(projects_env):
    client, api_key, _ = projects_env
    project_id = _project(client, api_key, "Not trashed")
    assert client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key)).status_code == 404
    assert client.post("/v1/projects/prj_nonexistent/restore", headers=_headers(api_key)).status_code == 404


@pytest.mark.slow
def test_a_trashed_project_is_gone_everywhere_else(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "trash/gone.jpg")
    other = _ingest_asset(client, api_key, library_id, "trash/gone-other.jpg")
    project_id = _project(client, api_key, "Gone", [asset_id])
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))

    h = _headers(api_key)
    assert client.get(f"/v1/projects/{project_id}/assets", headers=h).status_code == 404
    assert client.get(f"/v1/projects/{project_id}/export", params={"format": "fcp7"}, headers=h).status_code == 404
    assert client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [other]}, headers=h).status_code == 404
    assert client.patch(f"/v1/projects/{project_id}", json={"name": "Renamed"}, headers=h).status_code == 404
    assert client.delete(f"/v1/projects/{project_id}", headers=h).status_code == 404
    # The Swift apps still use the old paths.
    assert client.get(f"/v1/collections/{project_id}", headers=h).status_code == 404


@pytest.mark.slow
def test_the_old_collections_path_moves_to_the_trash_too(projects_env):
    client, api_key, _ = projects_env
    project_id = _project(client, api_key, "Deleted from the Mac app")
    assert client.delete(f"/v1/collections/{project_id}", headers=_headers(api_key)).status_code == 204
    assert project_id in _trashed(client, api_key)


@pytest.mark.slow
def test_a_trashed_public_link_stops_working_until_restored(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "trash/public.jpg")
    project_id = _project(client, api_key, "Public reel", [asset_id])
    client.patch(f"/v1/projects/{project_id}", json={"visibility": "public"}, headers=_headers(api_key))
    assert client.get(f"/v1/public/projects/{project_id}").status_code == 200

    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    assert client.get(f"/v1/public/projects/{project_id}").status_code == 404
    assert client.get(f"/v1/public/projects/{project_id}/assets").status_code == 404

    client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    assert client.get(f"/v1/public/projects/{project_id}").status_code == 200


@pytest.mark.slow
def test_empty_trash_deletes_only_the_chosen_projects_forever(projects_env):
    client, api_key, library_id = projects_env
    asset_id = _ingest_asset(client, api_key, library_id, "trash/forever.jpg")
    doomed = _project(client, api_key, "Doomed", [asset_id], visibility="public")
    kept = _project(client, api_key, "Kept in trash")
    assert client.get(f"/v1/public/projects/{doomed}").status_code == 200
    for pid in (doomed, kept):
        client.delete(f"/v1/projects/{pid}", headers=_headers(api_key))

    r = client.post("/v1/projects/empty-trash", json={"project_ids": [doomed]}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1}

    trashed = _trashed(client, api_key)
    assert doomed not in trashed and kept in trashed
    assert client.post(f"/v1/projects/{doomed}/restore", headers=_headers(api_key)).status_code == 404
    # Gone from the public index too, so the link can't reach the tenant.
    assert client.get(f"/v1/public/projects/{doomed}").status_code in (401, 404)
    # The clips themselves stay in the library.
    page = client.get(f"/v1/assets/page?library_id={library_id}", headers=_headers(api_key)).json()
    assert asset_id in [a["asset_id"] for a in page["items"]]


@pytest.mark.slow
def test_empty_trash_leaves_active_projects_and_other_peoples_trash(projects_env):
    client, api_key, _ = projects_env
    other_key = _other_user_key(client, api_key)
    mine = _project(client, api_key, "Mine, trashed")
    active = _project(client, api_key, "Mine, active")
    theirs = _project(client, other_key, "Theirs, trashed")
    client.delete(f"/v1/projects/{mine}", headers=_headers(api_key))
    client.delete(f"/v1/projects/{theirs}", headers=_headers(other_key))

    # Naming an active project or someone else's doesn't delete it.
    r = client.post("/v1/projects/empty-trash", json={"project_ids": [active, theirs]}, headers=_headers(api_key))
    assert r.json() == {"deleted": 0}
    r = client.post("/v1/projects/empty-trash", json={}, headers=_headers(api_key))
    assert r.status_code == 200, r.text

    assert mine not in _trashed(client, api_key)
    assert active in _ids(client, api_key)
    assert theirs in _trashed(client, other_key)


@pytest.mark.slow
def test_only_the_owner_trashes_restores_or_sees_the_trash(projects_env):
    client, api_key, _ = projects_env
    other_key = _other_user_key(client, api_key)
    project_id = _project(client, api_key, "Owner's shared project", visibility="shared")

    assert client.delete(f"/v1/projects/{project_id}", headers=_headers(other_key)).status_code == 403
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))

    assert project_id not in _ids(client, other_key)
    assert project_id not in _trashed(client, other_key)
    assert client.post(f"/v1/projects/{project_id}/restore", headers=_headers(other_key)).status_code == 404
    assert project_id in _trashed(client, api_key)
