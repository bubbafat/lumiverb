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
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made


def _ingest_asset(client, api_key, library_id, rel_path, media_type="image") -> str:
    """Helper: ingest a minimal asset, return asset_id."""
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    r = client.post(
        "/v1/ingest",
        data={
            "lineage": ingest_made(),
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "1000",
            "media_type": media_type,
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

    with PostgresContainer(PG_IMAGE) as control_postgres:
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

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
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
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "invalid_request"


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
        json={"asset_ids": [a1], "reason": "missing"},
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
        json={"asset_ids": [a1], "reason": "missing"},
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
        json={"asset_ids": [a1], "reason": "missing"},
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
        json={"asset_ids": [a1], "reason": "user", "remove_from_projects": True},
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
        json={"asset_ids": [a1], "reason": "missing"},
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
# Projects only: the pre-rename /collections paths are gone (Robert, Oct 9:
# the API as it should be, whatever the clients)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_pre_rename_collections_paths_are_gone(projects_env):
    client, api_key, _ = projects_env
    assert client.post("/v1/collections", json={"name": "Old client"}, headers=_headers(api_key)).status_code == 404
    project_id = _project(client, api_key, "No alias")
    assert client.get(f"/v1/collections/{project_id}", headers=_headers(api_key)).status_code == 404
    assert client.get(f"/v1/public/collections/{project_id}").status_code in (401, 404)
    item = client.get(f"/v1/projects/{project_id}", headers=_headers(api_key)).json()
    assert "collection_id" not in item


@pytest.mark.slow
@pytest.mark.parametrize("prefix", ["/v1/public/projects"])
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
            "media_type": "image", "lineage": ingest_made()}
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
def test_a_project_saved_from_a_search_pages_past_one_page(projects_env):
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": "SmartPaging", "root_path": "/smart-paging"},
        headers=_headers(api_key),
    ).json()["library_id"]
    expected = {_ingest_asset(client, api_key, lib, f"s/{i}.jpg") for i in range(5)}
    project_id = client.post(
        "/v1/projects",
        json={"name": "Saved search paging", "from_search": {"filters": [{"type": "library", "value": lib}]}},
        headers=_headers(api_key),
    ).json()["project_id"]

    ids = _all_pages(client, api_key, project_id, limit=2)

    assert len(ids) == len(set(ids)) == 5
    assert set(ids) == expected


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

    # A search saved as a project (its matches, in its order)
    project_id = client.post(
        "/v1/projects",
        json={"name": f"Mixed {direction}",
              "from_search": {"filters": [{"type": "library", "value": lib}],
                              "sort": "taken_at", "direction": direction}},
        headers=_headers(api_key),
    ).json()["project_id"]
    saved = _all_pages(client, api_key, project_id, limit=2)
    assert sorted(saved) == expected
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
    assert r.status_code == 200, r.text

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
    r = client.post("/v1/projects/empty-trash", json={"all": True}, headers=_headers(api_key))
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


# ---------------------------------------------------------------------------
# Clips leaving and coming back: a project links to clips; trashing a clip
# hides it (and says so), restoring brings it back in place, and deleting
# it for good removes it from every project, whatever state that's in.
# ---------------------------------------------------------------------------


def _clip_ids(client, api_key, project_id) -> list[str]:
    r = client.get(f"/v1/projects/{project_id}/assets", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return [a["asset_id"] for a in r.json()["items"]]


def _item(client, api_key, project_id) -> dict:
    r = client.get(f"/v1/projects/{project_id}", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return r.json()


def _trash_clips(client, api_key, *asset_ids, reason="user"):
    """reason "user": you trashed it (saying yes to the projects that use it).
    "missing": a scan found its file gone."""
    r = client.request(
        "DELETE", "/v1/assets",
        json={"asset_ids": list(asset_ids), "reason": reason, "remove_from_projects": reason == "user"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200, r.text


def _restore_clip(client, api_key, asset_id):
    assert client.post(f"/v1/assets/{asset_id}/restore", headers=_headers(api_key)).status_code == 204


def _delete_clips_for_good(client, api_key, *asset_ids):
    r = client.request(
        "DELETE", "/v1/trash/empty", json={"asset_ids": list(asset_ids), "remove_from_projects": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 200, r.text


def _export_trashed_header(client, api_key, project_id, which="Trashed") -> str | None:
    r = client.get(f"/v1/projects/{project_id}/export", params={"format": "fcp7"}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return r.headers.get(f"X-Lumiverb-Skipped-{which}")


@pytest.mark.slow
def test_a_trashed_clip_is_hidden_and_counted_until_restored(projects_env):
    client, api_key, library_id = projects_env
    a1, a2, a3 = (_ingest_asset(client, api_key, library_id, f"links/one-{i}.mov", "video") for i in range(3))
    project_id = _project(client, api_key, "One clip trashed", [a1, a2, a3])

    _trash_clips(client, api_key, a2)
    assert _clip_ids(client, api_key, project_id) == [a1, a3]
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"]) == (2, 1)
    assert _export_trashed_header(client, api_key, project_id) == "1"

    _restore_clip(client, api_key, a2)
    assert _clip_ids(client, api_key, project_id) == [a1, a2, a3]
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"]) == (3, 0)
    assert _export_trashed_header(client, api_key, project_id) == "0"


@pytest.mark.slow
def test_trashing_the_chosen_cover_clip_clears_the_choice(projects_env):
    # Robert, Oct 9: "Deleting the chosen clip goes back to the first rule
    # about default": the first active clip, until a new choice is made.
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/cover-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Chosen cover", [a1, a2])
    client.patch(f"/v1/projects/{project_id}", json={"cover_asset_id": a2}, headers=_headers(api_key))

    _trash_clips(client, api_key, a2)
    assert _item(client, api_key, project_id)["cover_asset_id"] == a1
    _restore_clip(client, api_key, a2)
    assert _item(client, api_key, project_id)["cover_asset_id"] == a1  # the choice went with it


@pytest.mark.slow
def test_a_chosen_cover_whose_file_goes_missing_is_shown_again_when_it_comes_back(projects_env):
    # Missing isn't a person deleting it: the first active clip shows meanwhile.
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/missing-cover-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Missing cover", [a1, a2])
    client.patch(f"/v1/projects/{project_id}", json={"cover_asset_id": a2}, headers=_headers(api_key))

    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [a2], "reason": "missing"}, headers=_headers(api_key))
    assert r.status_code in (200, 204), r.text
    assert _item(client, api_key, project_id)["cover_asset_id"] == a1
    assert _ingest_asset(client, api_key, library_id, "links/missing-cover-1.jpg") == a2  # the file is back
    assert _item(client, api_key, project_id)["cover_asset_id"] == a2


@pytest.mark.slow
def test_a_chosen_cover_clears_when_its_clip_leaves_the_project(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/removed-cover-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Removed cover", [a1, a2])
    client.patch(f"/v1/projects/{project_id}", json={"cover_asset_id": a2}, headers=_headers(api_key))

    client.request("DELETE", f"/v1/projects/{project_id}/assets", json={"asset_ids": [a2]}, headers=_headers(api_key))
    client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [a2]}, headers=_headers(api_key))
    assert _item(client, api_key, project_id)["cover_asset_id"] == a1


@pytest.mark.slow
def test_deleting_a_clip_for_good_removes_it_from_every_project(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "links/forever.jpg")
    other = _ingest_asset(client, api_key, library_id, "links/forever-other.jpg")
    active = _project(client, api_key, "Active", [clip, other])
    archived = _project(client, api_key, "Archived", [clip, other])
    trashed = _project(client, api_key, "Trashed", [clip, other])
    client.patch(f"/v1/projects/{archived}", json={"status": "archived"}, headers=_headers(api_key))
    client.delete(f"/v1/projects/{trashed}", headers=_headers(api_key))

    _trash_clips(client, api_key, clip)
    _delete_clips_for_good(client, api_key, clip)

    client.post(f"/v1/projects/{trashed}/restore", headers=_headers(api_key))
    for project_id in (active, archived, trashed):
        assert _clip_ids(client, api_key, project_id) == [other]
        item = _item(client, api_key, project_id)
        assert (item["asset_count"], item["trashed_asset_count"]) == (1, 0)


@pytest.mark.slow
def test_a_project_restored_after_its_clip_was_trashed(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/both-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Both in the trash", [a1, a2])
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    _trash_clips(client, api_key, a1)

    r = client.post(f"/v1/projects/{project_id}/restore", json={"with_clips": False}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"restored_clips": 0, "trashed_clips": 1, "missing_clips": 0, "archived_clips": 0}
    assert _clip_ids(client, api_key, project_id) == [a2]
    assert _item(client, api_key, project_id)["trashed_asset_count"] == 1

    _restore_clip(client, api_key, a1)
    assert _clip_ids(client, api_key, project_id) == [a1, a2]


@pytest.mark.slow
def test_a_project_whose_clips_are_all_trashed_is_empty_until_they_return(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/all-{i}.mov", "video") for i in range(2))
    project_id = _project(client, api_key, "All trashed", [a1, a2])

    _trash_clips(client, api_key, a1, a2)
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"], item["cover_asset_id"]) == (0, 2, None)
    assert _clip_ids(client, api_key, project_id) == []
    assert _export_trashed_header(client, api_key, project_id) == "2"

    for a in (a1, a2):
        _restore_clip(client, api_key, a)
    assert _clip_ids(client, api_key, project_id) == [a1, a2]


@pytest.mark.slow
def test_a_project_whose_clips_are_all_deleted_for_good_stays_empty(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/all-gone-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "All gone", [a1, a2])
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))

    _trash_clips(client, api_key, a1, a2)
    _delete_clips_for_good(client, api_key, a1, a2)

    assert _trashed(client, api_key)[project_id]["asset_count"] == 0
    client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"], item["cover_asset_id"]) == (0, 0, None)
    assert _clip_ids(client, api_key, project_id) == []


@pytest.mark.slow
def test_clips_whose_files_went_missing_are_counted_apart_from_trashed_ones(projects_env):
    client, api_key, library_id = projects_env
    a1, a2, a3 = (_ingest_asset(client, api_key, library_id, f"links/missing-{i}.mov", "video") for i in range(3))
    project_id = _project(client, api_key, "Drive offline", [a1, a2, a3])

    _trash_clips(client, api_key, a1)
    _trash_clips(client, api_key, a2, reason="missing")
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"], item["missing_asset_count"]) == (1, 1, 1)
    assert _export_trashed_header(client, api_key, project_id, "Trashed") == "1"
    assert _export_trashed_header(client, api_key, project_id, "Missing") == "1"


# ---------------------------------------------------------------------------
# Restoring a project's trashed clips, live or just restored from the trash
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_restore_clips_brings_back_the_ones_you_trashed(projects_env):
    client, api_key, library_id = projects_env
    a1, a2, a3, outside = (_ingest_asset(client, api_key, library_id, f"links/rc-{i}.jpg") for i in range(4))
    project_id = _project(client, api_key, "Restore clips", [a1, a2, a3])
    _trash_clips(client, api_key, a1, outside)
    _trash_clips(client, api_key, a2, reason="missing")

    r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"restored": 1, "missing": 1, "archived": 0}

    assert _clip_ids(client, api_key, project_id) == [a1, a3]
    item = _item(client, api_key, project_id)
    assert (item["trashed_asset_count"], item["missing_asset_count"]) == (0, 1)
    # A clip outside the project stays in the trash.
    assert client.get(f"/v1/assets/{outside}", headers=_headers(api_key)).status_code == 404
    # The restored clip is back in the library too.
    assert client.get(f"/v1/assets/{a1}", headers=_headers(api_key)).status_code == 200


@pytest.mark.slow
def test_restore_clips_after_restoring_the_project(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"links/rc-after-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Restore both", [a1, a2])
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    _trash_clips(client, api_key, a1, a2)

    # Not while the project itself is in the trash.
    assert client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(api_key)).status_code == 404
    client.post(f"/v1/projects/{project_id}/restore", json={"with_clips": False}, headers=_headers(api_key))
    assert _item(client, api_key, project_id)["trashed_asset_count"] == 2
    r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(api_key))
    assert r.json() == {"restored": 2, "missing": 0, "archived": 0}
    assert _clip_ids(client, api_key, project_id) == [a1, a2]


@pytest.mark.slow
def test_restore_clips_needs_someone_who_can_see_the_project(projects_env):
    client, api_key, library_id = projects_env
    other_key = _other_user_key(client, api_key)
    clip = _ingest_asset(client, api_key, library_id, "links/rc-private.jpg")
    project_id = _project(client, api_key, "Private", [clip])
    _trash_clips(client, api_key, clip)
    assert client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(other_key)).status_code == 404


# ---------------------------------------------------------------------------
# Which projects use these clips: what deleting them for good would touch
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_project_usage_lists_every_project_deleting_would_touch(projects_env):
    client, api_key, library_id = projects_env
    other_key = _other_user_key(client, api_key)
    a1, a2, unused = (_ingest_asset(client, api_key, library_id, f"links/usage-{i}.jpg") for i in range(3))
    active = _project(client, api_key, "Usage active", [a1, a2])
    archived = _project(client, api_key, "Usage archived", [a1])
    client.patch(f"/v1/projects/{archived}", json={"status": "archived"}, headers=_headers(api_key))
    trashed = _project(client, api_key, "Usage trashed", [a2])
    client.delete(f"/v1/projects/{trashed}", headers=_headers(api_key))
    _project(client, other_key, "Someone else's private one", [a1])

    r = client.post("/v1/assets/project-usage", json={"asset_ids": [a1, a2, unused]}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assets_in_projects"] == 2
    by_id = {p["project_id"]: p for p in body["projects"]}
    assert set(by_id) == {active, archived, trashed}
    assert (by_id[active]["clips"], by_id[active]["status"], by_id[active]["in_trash"]) == (2, "active", False)
    assert (by_id[archived]["clips"], by_id[archived]["status"]) == (1, "archived")
    assert by_id[trashed]["in_trash"] is True
    # Projects the caller can't see are counted, not named.
    assert body["other_projects"] == 1


@pytest.mark.slow
def test_project_usage_by_library(projects_env):
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": "Usage by library", "root_path": "/usage-lib"}, headers=_headers(api_key),
    ).json()["library_id"]
    a1, a2 = (_ingest_asset(client, api_key, lib, f"u/lib-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Uses the library", [a1, a2])

    r = client.post("/v1/assets/project-usage", json={"library_ids": [lib]}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json()["assets_in_projects"] == 2
    assert [(p["project_id"], p["clips"]) for p in r.json()["projects"]] == [(project_id, 2)]



# ---------------------------------------------------------------------------
# The API requires the user's decision, so every client has to ask for it
# ---------------------------------------------------------------------------


def _error(r) -> dict:
    return r.json()["error"]


@pytest.mark.slow
def test_deleting_clips_in_projects_for_good_needs_an_explicit_yes(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "intent/in-project.jpg")
    project_id = _project(client, api_key, "Intent: holds the clip", [clip])
    _trash_clips(client, api_key, clip)

    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clip]}, headers=_headers(api_key))
    assert r.status_code == 409, r.text
    err = _error(r)
    assert err["code"] == "in_projects"
    assert err["details"]["assets_in_projects"] == 1
    assert [p["project_id"] for p in err["details"]["projects"]] == [project_id]
    assert _item(client, api_key, project_id)["trashed_asset_count"] == 1  # nothing deleted

    r = client.request(
        "DELETE", "/v1/trash/empty", json={"asset_ids": [clip], "remove_from_projects": True}, headers=_headers(api_key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1
    assert _item(client, api_key, project_id)["trashed_asset_count"] == 0


@pytest.mark.slow
def test_deleting_clips_in_no_project_needs_no_yes(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "intent/loose.jpg")
    _trash_clips(client, api_key, clip)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clip]}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1


@pytest.mark.slow
def test_emptying_the_whole_clip_trash_asks_too(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "intent/whole-trash.jpg")
    _project(client, api_key, "Intent: whole trash", [clip])
    _trash_clips(client, api_key, clip)
    r = client.request("DELETE", "/v1/trash/empty", json={"all": True}, headers=_headers(api_key))
    assert r.status_code == 409, r.text
    assert _error(r)["code"] == "in_projects"
    assert client.post(f"/v1/assets/{clip}/restore", headers=_headers(api_key)).status_code == 204


@pytest.mark.slow
def test_emptying_library_trash_with_clips_in_projects_needs_a_yes(projects_env):
    client, api_key, _ = projects_env
    lib = client.post(
        "/v1/libraries", json={"name": "Intent lib", "root_path": "/intent-lib"}, headers=_headers(api_key),
    ).json()["library_id"]
    clip = _ingest_asset(client, api_key, lib, "intent/lib-clip.jpg")
    project_id = _project(client, api_key, "Intent: library clip", [clip])
    assert client.request("DELETE", f"/v1/libraries/{lib}", json={"remove_from_projects": True},
                          headers=_headers(api_key)).status_code == 204

    r = client.post("/v1/libraries/empty-trash", json={"all": True}, headers=_headers(api_key))
    assert r.status_code == 409, r.text
    assert _error(r)["code"] == "in_projects"
    assert _error(r)["details"]["assets_in_projects"] == 1

    r = client.post("/v1/libraries/empty-trash", json={"all": True, "remove_from_projects": True}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] >= 1
    assert _item(client, api_key, project_id)["asset_count"] == 0


@pytest.mark.slow
def test_restoring_a_project_with_trashed_clips_needs_a_choice(projects_env):
    client, api_key, library_id = projects_env
    a1, a2 = (_ingest_asset(client, api_key, library_id, f"intent/choice-{i}.jpg") for i in range(2))
    project_id = _project(client, api_key, "Intent: restore choice", [a1, a2])
    _trash_clips(client, api_key, a1)
    _trash_clips(client, api_key, a2, reason="missing")
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))

    r = client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    assert r.status_code == 409, r.text
    assert _error(r)["code"] == "clips_in_trash"
    assert _error(r)["details"] == {"trashed_clips": 1, "missing_clips": 1}
    assert project_id in _trashed(client, api_key)  # still in the trash

    r = client.post(f"/v1/projects/{project_id}/restore", json={"with_clips": True}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"restored_clips": 1, "trashed_clips": 0, "missing_clips": 1, "archived_clips": 0}
    assert _clip_ids(client, api_key, project_id) == [a1]


@pytest.mark.slow
def test_restoring_a_project_without_trashed_clips_needs_no_choice(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "intent/no-choice.jpg")
    project_id = _project(client, api_key, "Intent: nothing to ask", [clip])
    _trash_clips(client, api_key, clip, reason="missing")  # missing files can't be restored
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    r = client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"restored_clips": 0, "trashed_clips": 0, "missing_clips": 1, "archived_clips": 0}



# ---------------------------------------------------------------------------
# Review round 1: trashed libraries, roles, and the remaining edge cases
# ---------------------------------------------------------------------------


def _viewer_key(client, api_key) -> str:
    r = client.post("/v1/keys", json={"label": "viewer", "role": "viewer"}, headers=_headers(api_key))
    assert r.status_code in (200, 201), r.text
    return r.json()["plaintext"]


def _library(client, api_key, name) -> str:
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/{name}"}, headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


@pytest.mark.slow
def test_clips_in_a_trashed_library_are_counted_apart_and_come_back_with_it(projects_env):
    client, api_key, _ = projects_env
    lib = _library(client, api_key, "Trashed-library-clips")
    trashed_first, other = (_ingest_asset(client, api_key, lib, f"tl/{i}.mov", "video") for i in range(2))
    project_id = _project(client, api_key, "Uses a deleted library", [trashed_first, other])
    _trash_clips(client, api_key, trashed_first)  # trashed by a person before the library went
    assert client.request("DELETE", f"/v1/libraries/{lib}", json={"remove_from_projects": True},
                          headers=_headers(api_key)).status_code == 204

    item = _item(client, api_key, project_id)
    assert (item["trashed_asset_count"], item["missing_asset_count"], item["library_trashed_asset_count"]) == (0, 0, 2)
    assert _export_trashed_header(client, api_key, project_id, "Library-Trashed") == "2"

    r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(api_key))
    assert r.json()["restored"] == 0
    assert client.get(f"/v1/assets/{trashed_first}", headers=_headers(api_key)).status_code == 404
    # Nothing restorable, so restoring the project needs no choice.
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    assert client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key)).status_code == 200

    # Restoring the library brings back the clip that went with it; the one a
    # person trashed first stays in the trash.
    assert client.post(f"/v1/libraries/{lib}/restore", headers=_headers(api_key)).status_code == 200
    item = _item(client, api_key, project_id)
    assert (item["asset_count"], item["trashed_asset_count"], item["library_trashed_asset_count"]) == (1, 1, 0)


@pytest.mark.slow
def test_export_notes_count_photos_too(projects_env):
    # Photos export as stills (Robert, Oct 9), so a trashed one is left out like a video.
    client, api_key, library_id = projects_env
    photo = _ingest_asset(client, api_key, library_id, "videos-only/photo.jpg")
    video = _ingest_asset(client, api_key, library_id, "videos-only/clip.mov", "video")
    project_id = _project(client, api_key, "Photo and video in the trash", [photo, video])
    _trash_clips(client, api_key, photo, video)
    assert _export_trashed_header(client, api_key, project_id) == "2"


@pytest.mark.slow
def test_viewers_cannot_trash_or_restore_clips(projects_env):
    client, api_key, library_id = projects_env
    viewer = _viewer_key(client, api_key)
    clip = _ingest_asset(client, api_key, library_id, "roles/clip.jpg")
    project_id = _project(client, api_key, "Roles", [clip], visibility="shared")
    assert client.delete(f"/v1/assets/{clip}", headers=_headers(viewer)).status_code == 403
    assert client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"},
                          headers=_headers(viewer)).status_code == 403
    _trash_clips(client, api_key, clip)
    assert client.post(f"/v1/assets/{clip}/restore", headers=_headers(viewer)).status_code == 403
    assert client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(viewer)).status_code == 403


@pytest.mark.slow
def test_marking_files_missing_needs_an_editor(projects_env):
    # The scanner marks files it no longer finds. Scanning (ingest too) is an
    # editor's: a viewer can't take clips out of sight either way.
    client, api_key, library_id = projects_env
    viewer = _viewer_key(client, api_key)
    clip = _ingest_asset(client, api_key, library_id, "roles/gone-from-disk.jpg")
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "missing"},
                       headers=_headers(viewer))
    assert r.status_code == 403, r.text
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "missing"},
                       headers=_headers(api_key))
    assert r.status_code == 200, r.text
    assert r.json()["trashed"] == [clip]


@pytest.mark.slow
def test_a_scan_cant_bring_back_clips_of_a_trashed_library(projects_env):
    client, api_key, _ = projects_env
    lib = _library(client, api_key, "Trashed-then-scanned")
    clip = _ingest_asset(client, api_key, lib, "scan/old.jpg")
    assert client.delete(f"/v1/libraries/{lib}", headers=_headers(api_key)).status_code == 204

    import io

    from PIL import Image as PILImage

    for rel_path in ("scan/old.jpg", "scan/new.jpg"):
        buf = io.BytesIO()
        PILImage.new("RGB", (10, 10)).save(buf, format="JPEG")
        buf.seek(0)
        r = client.post(
            "/v1/ingest",
            data={"library_id": lib, "rel_path": rel_path, "file_size": "1000", "media_type": "image",
                  "lineage": ingest_made()},
            files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
            headers=_headers(api_key),
        )
        assert r.status_code == 409, (rel_path, r.text)
    assert client.get(f"/v1/assets/{clip}", headers=_headers(api_key)).status_code == 404


@pytest.mark.slow
def test_another_editor_can_restore_clips_in_a_shared_project(projects_env):
    client, api_key, library_id = projects_env
    other_key = _other_user_key(client, api_key)
    clip = _ingest_asset(client, api_key, library_id, "roles/shared.jpg")
    project_id = _project(client, api_key, "Shared, clip trashed", [clip], visibility="shared")
    _trash_clips(client, api_key, clip)
    r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=_headers(other_key))
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == 1


@pytest.mark.slow
def test_a_trashed_public_projects_thumbnails_are_gone_until_restored(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "public/thumb.jpg")
    project_id = _project(client, api_key, "Public thumbs", [clip], visibility="public")
    url = f"/v1/assets/{clip}/artifacts/thumbnail?public_project_id={project_id}"
    assert client.get(url).status_code == 200
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    assert client.get(url).status_code == 404
    client.post(f"/v1/projects/{project_id}/restore", headers=_headers(api_key))
    assert client.get(url).status_code == 200


@pytest.mark.slow
def test_deleting_a_clip_only_a_trashed_project_holds_still_asks(projects_env):
    client, api_key, library_id = projects_env
    clip = _ingest_asset(client, api_key, library_id, "intent/trashed-project-only.jpg")
    project_id = _project(client, api_key, "Only holder, trashed", [clip])
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    _trash_clips(client, api_key, clip)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clip]}, headers=_headers(api_key))
    assert r.status_code == 409, r.text
    assert [p["in_trash"] for p in _error(r)["details"]["projects"]] == [True]


@pytest.mark.slow
def test_usage_counts_but_doesnt_name_someone_elses_trashed_shared_project(projects_env):
    client, api_key, library_id = projects_env
    other_key = _other_user_key(client, api_key)
    clip = _ingest_asset(client, api_key, library_id, "usage/others-trashed.jpg")
    theirs = _project(client, other_key, "Theirs, shared then trashed", [clip], visibility="shared")
    client.delete(f"/v1/projects/{theirs}", headers=_headers(other_key))
    body = client.post("/v1/assets/project-usage", json={"asset_ids": [clip]}, headers=_headers(api_key)).json()
    assert (body["projects"], body["other_projects"]) == ([], 1)


@pytest.mark.slow
def test_empty_trash_needs_no_body(projects_env):
    client, api_key, _ = projects_env
    r = client.post("/v1/projects/empty-trash", json={"all": True}, headers=_headers(api_key))
    assert r.status_code == 200, r.text


@pytest.mark.slow
def test_trashing_a_project_updates_it(projects_env):
    client, api_key, _ = projects_env
    project_id = _project(client, api_key, "Updated when trashed")
    before = _item(client, api_key, project_id)["updated_at"]
    client.delete(f"/v1/projects/{project_id}", headers=_headers(api_key))
    trashed = _trashed(client, api_key)[project_id]
    assert trashed["updated_at"] > before
    assert trashed["deleted_at"] >= trashed["updated_at"][:19]


@pytest.mark.slow
@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.jpg", "a/../../outside.jpg"])
def test_writes_refuse_rel_paths_outside_the_library(projects_env, bad):
    """upsert, batch-moves and ingest share one rule: relative, no "..", no leading "/"."""
    client, api_key, library_id = projects_env
    h = _headers(api_key)
    r = client.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": bad, "file_size": 1,
                                               "file_mtime": None, "media_type": "image"}, headers=h)
    assert r.status_code == 400, r.text
    clip = _ingest_asset(client, api_key, library_id, f"relpath/{len(bad)}-{bad.count('/')}.jpg")
    r = client.post("/v1/assets/batch-moves", json={"items": [{"asset_id": clip, "rel_path": bad}]}, headers=h)
    assert r.status_code == 400, r.text
    assert client.get(f"/v1/assets/{clip}", headers=h).json()["rel_path"].startswith("relpath/")
    with pytest.raises(AssertionError, match="400"):
        _ingest_asset(client, api_key, library_id, bad)
