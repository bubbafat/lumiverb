"""Smart (dynamic) projects API tests.

Smart projects store a saved query as a filter algebra tree:
  {"filters": [{"type": "camera_make", "value": "Canon"}, ...], "sort": "taken_at", "direction": "desc"}

When viewed, they return live results by executing the saved query via the
unified query_page() code path. Text search filters use the candidate-set
pattern (Quickwit → candidate IDs → SQL).

Assets cannot be manually added to or removed from a smart project.
"""

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


def _ingest_asset(
    client,
    api_key,
    library_id,
    rel_path,
    *,
    exif_data=None,
    vision_data=None,
) -> str:
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    data = {
        "library_id": library_id,
        "rel_path": rel_path,
        "file_size": "1000",
        "media_type": "image",
        "width": "100",
        "height": "100",
    }
    if exif_data is not None:
        data["exif"] = json.dumps(exif_data)
    if vision_data is not None:
        data["vision"] = json.dumps(vision_data)

    r = client.post(
        "/v1/ingest",
        data=data,
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def smart_env():
    """Testcontainers env for smart project tests."""
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
        os.environ["ADMIN_KEY"] = "test-admin-smartcol"
        os.environ["JWT_SECRET"] = "test-jwt-secret-smartcol"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SmartColTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-smartcol"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

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
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "SmartLib", "root_path": "/tmp/smart-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Create smart project
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_smart_project(smart_env):
    """Create a smart project with a saved query using filter algebra."""
    client, api_key, library_id = smart_env

    saved_query = {
        "filters": [
            {"type": "camera_make", "value": "Canon"},
            {"type": "stars", "value": "3+"},
            {"type": "favorite", "value": "yes"},
            {"type": "library", "value": library_id},
        ],
        "sort": "taken_at",
        "direction": "desc",
    }

    r = client.post(
        "/v1/projects",
        json={
            "name": "Best Canon Shots",
            "type": "smart",
            "saved_query": saved_query,
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    data = r.json()
    assert data["name"] == "Best Canon Shots"
    assert data["type"] == "smart"
    assert data["saved_query"] is not None
    assert data["saved_query"]["filters"][0]["type"] == "camera_make"


@pytest.mark.slow
def test_create_smart_project_with_search_query(smart_env):
    """Smart project can include a text search query filter."""
    client, api_key, library_id = smart_env

    saved_query = {
        "filters": [
            {"type": "query", "value": "sunset"},
            {"type": "color", "value": "orange"},
        ],
    }

    r = client.post(
        "/v1/projects",
        json={
            "name": "Orange Sunsets",
            "type": "smart",
            "saved_query": saved_query,
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["type"] == "smart"
    # Verify the search query filter is stored
    filter_types = [f["type"] for f in data["saved_query"]["filters"]]
    assert "query" in filter_types


@pytest.mark.slow
def test_create_smart_project_without_saved_query_rejected(smart_env):
    """A smart project must have a saved_query."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Empty Smart",
            "type": "smart",
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_create_static_project_with_saved_query_rejected(smart_env):
    """A static project must NOT have a saved_query."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Bad Static",
            "type": "static",
            "saved_query": {"filters": [{"type": "camera_make", "value": "Canon"}]},
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# List includes type
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_list_projects_includes_type(smart_env):
    """Project list response includes the type field."""
    client, api_key, library_id = smart_env

    client.post(
        "/v1/projects",
        json={"name": "Static One"},
        headers=_headers(api_key),
    )
    client.post(
        "/v1/projects",
        json={
            "name": "Smart One",
            "type": "smart",
            "saved_query": {"filters": [{"type": "favorite", "value": "yes"}]},
        },
        headers=_headers(api_key),
    )

    r = client.get("/v1/projects", headers=_headers(api_key))
    assert r.status_code == 200
    items = r.json()["items"]
    types = {i["name"]: i["type"] for i in items if i["name"] in ("Static One", "Smart One")}
    assert types.get("Static One") == "static"
    assert types.get("Smart One") == "smart"


# ---------------------------------------------------------------------------
# Get smart project detail returns saved_query
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_get_smart_project_returns_saved_query(smart_env):
    """GET /projects/{id} for a smart project includes saved_query."""
    client, api_key, library_id = smart_env

    saved_query = {
        "filters": [
            {"type": "has_gps", "value": "yes"},
            {"type": "media", "value": "image"},
        ],
    }
    r = client.post(
        "/v1/projects",
        json={"name": "Geotagged Photos", "type": "smart", "saved_query": saved_query},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    data = r2.json()
    assert data["type"] == "smart"
    filter_types = [f["type"] for f in data["saved_query"]["filters"]]
    assert "has_gps" in filter_types
    assert "media" in filter_types


@pytest.mark.slow
def test_get_static_project_has_null_saved_query(smart_env):
    """GET /projects/{id} for a static project has saved_query=null."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={"name": "Plain Static"},
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    assert r2.json()["type"] == "static"
    assert r2.json()["saved_query"] is None


# ---------------------------------------------------------------------------
# Smart project assets endpoint returns live results
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_project_assets_returns_live_results(smart_env):
    """GET /projects/{id}/assets for a smart project executes the saved query."""
    client, api_key, library_id = smart_env

    a_canon = _ingest_asset(
        client, api_key, library_id, "smart_canon.jpg",
        exif_data={"camera_make": "Canon", "taken_at": "2024-06-01T10:00:00+00:00"},
    )
    a_sony = _ingest_asset(
        client, api_key, library_id, "smart_sony.jpg",
        exif_data={"camera_make": "Sony", "taken_at": "2024-06-02T10:00:00+00:00"},
    )

    r = client.post(
        "/v1/projects",
        json={
            "name": "Canon Only",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Canon"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_canon in ids
    assert a_sony not in ids


@pytest.mark.slow
def test_smart_project_updates_automatically(smart_env):
    """New assets matching the query appear in the smart project automatically."""
    client, api_key, library_id = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Auto-Update Test",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Fujifilm"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    assert len(r2.json()["items"]) == 0

    a_fuji = _ingest_asset(
        client, api_key, library_id, "smart_fuji.jpg",
        exif_data={"camera_make": "Fujifilm", "taken_at": "2024-07-01T10:00:00+00:00"},
    )

    r3 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r3.status_code == 200
    ids = [i["asset_id"] for i in r3.json()["items"]]
    assert a_fuji in ids


# ---------------------------------------------------------------------------
# Smart project: no manual add/remove
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_project_add_assets_rejected(smart_env):
    """POST /projects/{id}/assets is rejected for smart projects."""
    client, api_key, library_id = smart_env

    a1 = _ingest_asset(client, api_key, library_id, "smart_noadd.jpg")

    r = client.post(
        "/v1/projects",
        json={
            "name": "No Add Test",
            "type": "smart",
            "saved_query": {"filters": [{"type": "favorite", "value": "yes"}]},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.post(
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


@pytest.mark.slow
def test_smart_project_remove_assets_rejected(smart_env):
    """DELETE /projects/{id}/assets is rejected for smart projects."""
    client, api_key, library_id = smart_env

    a1 = _ingest_asset(client, api_key, library_id, "smart_noremove.jpg")

    r = client.post(
        "/v1/projects",
        json={
            "name": "No Remove Test",
            "type": "smart",
            "saved_query": {"filters": [{"type": "has_gps", "value": "yes"}]},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.request(
        "DELETE",
        f"/v1/projects/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


@pytest.mark.slow
def test_smart_project_reorder_rejected(smart_env):
    """PATCH /projects/{id}/reorder is rejected for smart projects."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "No Reorder Test",
            "type": "smart",
            "saved_query": {"filters": [{"type": "media", "value": "video"}]},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.patch(
        f"/v1/projects/{col_id}/reorder",
        json={"asset_ids": []},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Update smart project saved_query
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_update_smart_project_saved_query(smart_env):
    """PATCH can update the saved_query of a smart project."""
    client, api_key, library_id = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Updatable Smart",
            "type": "smart",
            "saved_query": {"filters": [{"type": "camera_make", "value": "Canon"}]},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.patch(
        f"/v1/projects/{col_id}",
        json={"saved_query": {"filters": [{"type": "camera_make", "value": "Sony"}]}},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    filters = r2.json()["saved_query"]["filters"]
    assert any(f["type"] == "camera_make" and f["value"] == "Sony" for f in filters)


# ---------------------------------------------------------------------------
# Smart project asset_count reflects live count
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_project_asset_count_is_live(smart_env):
    """asset_count on a smart project reflects the current matching count."""
    client, api_key, library_id = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Count Test",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Leica"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 0

    _ingest_asset(
        client, api_key, library_id, "smart_leica.jpg",
        exif_data={"camera_make": "Leica", "taken_at": "2024-08-01T10:00:00+00:00"},
    )

    r3 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Default type is "static"
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_default_project_type_is_static(smart_env):
    """Projects created without type field default to static."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={"name": "Default Type"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    assert r.json()["type"] == "static"


# ---------------------------------------------------------------------------
# Smart project with rating filters
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_project_e2e_browse_then_create(smart_env):
    """End-to-end: query with filters, confirm results, create smart project, verify match.

    1. Ingest assets with different cameras
    2. Query with camera_make:Nikon filter → get N results
    3. Create smart project from same filters
    4. List project assets → must return the same N results
    """
    client, api_key, library_id = smart_env

    a_nikon1 = _ingest_asset(
        client, api_key, library_id, "e2e_nikon1.jpg",
        exif_data={"camera_make": "Nikon", "taken_at": "2024-01-01T10:00:00+00:00"},
    )
    a_nikon2 = _ingest_asset(
        client, api_key, library_id, "e2e_nikon2.jpg",
        exif_data={"camera_make": "Nikon", "taken_at": "2024-01-02T10:00:00+00:00"},
    )
    a_canon = _ingest_asset(
        client, api_key, library_id, "e2e_canon.jpg",
        exif_data={"camera_make": "Canon", "taken_at": "2024-01-03T10:00:00+00:00"},
    )

    # Step 1: Query with camera_make:Nikon + library scope
    r_query = client.get(
        f"/v1/query?f=camera_make:Nikon&f=library:{library_id}",
        headers=_headers(api_key),
    )
    assert r_query.status_code == 200
    query_ids = [i["asset_id"] for i in r_query.json()["items"]]
    assert a_nikon1 in query_ids
    assert a_nikon2 in query_ids
    assert a_canon not in query_ids

    # Step 2: Create smart project with the SAME filters
    r_create = client.post(
        "/v1/projects",
        json={
            "name": "E2E Nikon Project",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Nikon"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    assert r_create.status_code == 201
    col_id = r_create.json()["project_id"]

    # Step 3: List project assets — must match query results
    r_assets = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r_assets.status_code == 200
    col_ids = [i["asset_id"] for i in r_assets.json()["items"]]
    assert a_nikon1 in col_ids, f"Nikon1 missing from smart project. Got: {col_ids}"
    assert a_nikon2 in col_ids, f"Nikon2 missing from smart project. Got: {col_ids}"
    assert a_canon not in col_ids, "Canon should not be in Nikon smart project"

    # Step 4: asset_count on the project detail must match
    r_detail = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r_detail.status_code == 200
    assert r_detail.json()["asset_count"] >= 2


@pytest.mark.slow
def test_smart_project_e2e_queryparams_style_filters(smart_env):
    """Filter algebra round-trip: create project from filter tree, evaluate live."""
    client, api_key, library_id = smart_env

    a1 = _ingest_asset(
        client, api_key, library_id, "e2e_qp_match.jpg",
        exif_data={"camera_make": "Pentax", "taken_at": "2024-05-01T10:00:00+00:00"},
    )
    a2 = _ingest_asset(
        client, api_key, library_id, "e2e_qp_nomatch.jpg",
        exif_data={"camera_make": "Olympus", "taken_at": "2024-05-02T10:00:00+00:00"},
    )
    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))

    r = client.post(
        "/v1/projects",
        json={
            "name": "E2E Pentax Favorites",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Pentax"},
                    {"type": "favorite", "value": "yes"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a1 in ids, f"Favorited Pentax asset missing. Got: {ids}"
    assert a2 not in ids, "Non-matching asset should not appear"


@pytest.mark.slow
def test_smart_project_saved_query_is_visible(smart_env):
    """GET project detail returns saved_query so the UI can display filters."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/projects",
        json={
            "name": "Visible Filters",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "camera_make", "value": "Sony"},
                    {"type": "stars", "value": "4+"},
                    {"type": "color", "value": "red"},
                ],
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    sq = r2.json()["saved_query"]
    assert sq is not None, "saved_query must be returned in project detail"
    filter_types = {f["type"]: f["value"] for f in sq["filters"]}
    assert filter_types["camera_make"] == "Sony"
    assert filter_types["stars"] == "4+"
    assert filter_types["color"] == "red"


@pytest.mark.slow
def test_smart_project_rating_filters(smart_env):
    """Smart project with star + color filters returns matching assets."""
    client, api_key, library_id = smart_env

    a_match = _ingest_asset(client, api_key, library_id, "smart_rated_match.jpg",
        exif_data={"taken_at": "2024-09-01T10:00:00+00:00"})
    a_nomatch = _ingest_asset(client, api_key, library_id, "smart_rated_nomatch.jpg",
        exif_data={"taken_at": "2024-09-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_match}/rating", json={"stars": 5, "color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_nomatch}/rating", json={"stars": 2}, headers=_headers(api_key))

    r = client.post(
        "/v1/projects",
        json={
            "name": "Top Red Picks",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "stars", "value": "4+"},
                    {"type": "color", "value": "red"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_match in ids
    assert a_nomatch not in ids


# ---------------------------------------------------------------------------
# Smart project with text search (candidate-set pattern)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_project_with_search_term_uses_candidate_set(smart_env):
    """Smart project with query filter uses Quickwit candidate-set pattern.

    This is the core bug fix: search "Disney" + camera_make:Canon → save as
    smart project → project must show only Canon assets matching "Disney",
    not all assets or only the search results.

    Mock Quickwit to return specific candidates, then verify the structured
    filter (camera_make) narrows within those candidates.
    """
    from unittest.mock import MagicMock, patch

    client, api_key, library_id = smart_env

    a_canon = _ingest_asset(
        client, api_key, library_id, "search_canon.jpg",
        exif_data={"camera_make": "Canon", "taken_at": "2024-11-01T10:00:00+00:00"},
    )
    a_sony = _ingest_asset(
        client, api_key, library_id, "search_sony.jpg",
        exif_data={"camera_make": "Sony", "taken_at": "2024-11-02T10:00:00+00:00"},
    )

    # Create smart project with search + structured filter
    r = client.post(
        "/v1/projects",
        json={
            "name": "Disney Canon Project",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "query", "value": "Disney"},
                    {"type": "camera_make", "value": "Canon"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    col_id = r.json()["project_id"]

    # Mock Quickwit to return both assets as text search hits
    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = [
        {"asset_id": a_canon, "score": 0.95},
        {"asset_id": a_sony, "score": 0.80},
    ]
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    # Only Canon survives: text search returned both, but camera_make:Canon filters Sony out
    assert a_canon in ids, f"Canon asset missing from search+filter project. Got: {ids}"
    assert a_sony not in ids, "Sony should be excluded by camera_make:Canon filter"


@pytest.mark.slow
def test_smart_project_search_term_no_results(smart_env):
    """Smart project with search term returns empty when Quickwit returns no hits."""
    from unittest.mock import MagicMock, patch

    client, api_key, library_id = smart_env

    _ingest_asset(
        client, api_key, library_id, "search_empty.jpg",
        exif_data={"camera_make": "Canon"},
    )

    r = client.post(
        "/v1/projects",
        json={
            "name": "No Hits Project",
            "type": "smart",
            "saved_query": {
                "filters": [
                    {"type": "query", "value": "xyznonexistent"},
                    {"type": "library", "value": library_id},
                ],
            },
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    col_id = r.json()["project_id"]

    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = []
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    assert r2.json()["items"] == []
