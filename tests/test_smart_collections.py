"""Smart (dynamic) collections API tests.

Smart collections store a saved query (filters + optional search text).
When viewed, they return live results by executing the saved query.
Assets cannot be manually added to or removed from a smart collection.

All tests here are expected to FAIL until the server-side implementation
is complete (model, migration, API changes).
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
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


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
    """Testcontainers env for smart collection tests."""
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

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
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
# Create smart collection
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_smart_collection(smart_env):
    """Create a smart collection with a saved query."""
    client, api_key, library_id = smart_env

    saved_query = {
        "filters": {
            "camera_make": "Canon",
            "star_min": 3,
            "favorite": True,
        },
        "library_id": library_id,
    }

    r = client.post(
        "/v1/collections",
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
    assert data["saved_query"]["filters"]["camera_make"] == "Canon"


@pytest.mark.slow
def test_create_smart_collection_with_search_query(smart_env):
    """Smart collection can include a text search query."""
    client, api_key, library_id = smart_env

    saved_query = {
        "q": "sunset",
        "filters": {
            "color": "orange",
        },
    }

    r = client.post(
        "/v1/collections",
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
    assert data["saved_query"]["q"] == "sunset"


@pytest.mark.slow
def test_create_smart_collection_without_saved_query_rejected(smart_env):
    """A smart collection must have a saved_query."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "Empty Smart",
            "type": "smart",
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_create_static_collection_with_saved_query_rejected(smart_env):
    """A static collection must NOT have a saved_query."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "Bad Static",
            "type": "static",
            "saved_query": {"filters": {"camera_make": "Canon"}},
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# List includes type
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_list_collections_includes_type(smart_env):
    """Collection list response includes the type field."""
    client, api_key, library_id = smart_env

    # Create one static and one smart
    client.post(
        "/v1/collections",
        json={"name": "Static One"},
        headers=_headers(api_key),
    )
    client.post(
        "/v1/collections",
        json={
            "name": "Smart One",
            "type": "smart",
            "saved_query": {"filters": {"favorite": True}},
        },
        headers=_headers(api_key),
    )

    r = client.get("/v1/collections", headers=_headers(api_key))
    assert r.status_code == 200
    items = r.json()["items"]
    types = {i["name"]: i["type"] for i in items if i["name"] in ("Static One", "Smart One")}
    assert types.get("Static One") == "static"
    assert types.get("Smart One") == "smart"


# ---------------------------------------------------------------------------
# Get smart collection detail returns saved_query
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_get_smart_collection_returns_saved_query(smart_env):
    """GET /collections/{id} for a smart collection includes saved_query."""
    client, api_key, library_id = smart_env

    saved_query = {
        "filters": {"has_gps": True, "media_type": "image"},
        "library_id": library_id,
    }
    r = client.post(
        "/v1/collections",
        json={"name": "Geotagged Photos", "type": "smart", "saved_query": saved_query},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    data = r2.json()
    assert data["type"] == "smart"
    assert data["saved_query"]["filters"]["has_gps"] is True


@pytest.mark.slow
def test_get_static_collection_has_null_saved_query(smart_env):
    """GET /collections/{id} for a static collection has saved_query=null."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={"name": "Plain Static"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    assert r2.json()["type"] == "static"
    assert r2.json()["saved_query"] is None


# ---------------------------------------------------------------------------
# Smart collection assets endpoint returns live results
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_collection_assets_returns_live_results(smart_env):
    """GET /collections/{id}/assets for a smart collection executes the saved query."""
    client, api_key, library_id = smart_env

    # Ingest assets with different cameras
    a_canon = _ingest_asset(
        client, api_key, library_id, "smart_canon.jpg",
        exif_data={"camera_make": "Canon", "taken_at": "2024-06-01T10:00:00+00:00"},
    )
    a_sony = _ingest_asset(
        client, api_key, library_id, "smart_sony.jpg",
        exif_data={"camera_make": "Sony", "taken_at": "2024-06-02T10:00:00+00:00"},
    )

    # Create smart collection for Canon only
    r = client.post(
        "/v1/collections",
        json={
            "name": "Canon Only",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Canon"},
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Get assets — should return only Canon
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_canon in ids
    assert a_sony not in ids


@pytest.mark.slow
def test_smart_collection_updates_automatically(smart_env):
    """New assets matching the query appear in the smart collection automatically."""
    client, api_key, library_id = smart_env

    # Create smart collection first
    r = client.post(
        "/v1/collections",
        json={
            "name": "Auto-Update Test",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Fujifilm"},
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Initially empty
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    assert len(r2.json()["items"]) == 0

    # Ingest a matching asset
    a_fuji = _ingest_asset(
        client, api_key, library_id, "smart_fuji.jpg",
        exif_data={"camera_make": "Fujifilm", "taken_at": "2024-07-01T10:00:00+00:00"},
    )

    # Now the collection should include it
    r3 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r3.status_code == 200
    ids = [i["asset_id"] for i in r3.json()["items"]]
    assert a_fuji in ids


# ---------------------------------------------------------------------------
# Smart collection: no manual add/remove
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_collection_add_assets_rejected(smart_env):
    """POST /collections/{id}/assets is rejected for smart collections."""
    client, api_key, library_id = smart_env

    a1 = _ingest_asset(client, api_key, library_id, "smart_noadd.jpg")

    r = client.post(
        "/v1/collections",
        json={
            "name": "No Add Test",
            "type": "smart",
            "saved_query": {"filters": {"favorite": True}},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.post(
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


@pytest.mark.slow
def test_smart_collection_remove_assets_rejected(smart_env):
    """DELETE /collections/{id}/assets is rejected for smart collections."""
    client, api_key, library_id = smart_env

    a1 = _ingest_asset(client, api_key, library_id, "smart_noremove.jpg")

    r = client.post(
        "/v1/collections",
        json={
            "name": "No Remove Test",
            "type": "smart",
            "saved_query": {"filters": {"has_gps": True}},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.request(
        "DELETE",
        f"/v1/collections/{col_id}/assets",
        json={"asset_ids": [a1]},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


@pytest.mark.slow
def test_smart_collection_reorder_rejected(smart_env):
    """PATCH /collections/{id}/reorder is rejected for smart collections."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "No Reorder Test",
            "type": "smart",
            "saved_query": {"filters": {"media_type": "video"}},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.patch(
        f"/v1/collections/{col_id}/reorder",
        json={"asset_ids": []},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400
    assert "smart" in r2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Update smart collection saved_query
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_update_smart_collection_saved_query(smart_env):
    """PATCH can update the saved_query of a smart collection."""
    client, api_key, library_id = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "Updatable Smart",
            "type": "smart",
            "saved_query": {"filters": {"camera_make": "Canon"}},
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"saved_query": {"filters": {"camera_make": "Sony"}}},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["saved_query"]["filters"]["camera_make"] == "Sony"


# ---------------------------------------------------------------------------
# Smart collection asset_count reflects live count
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_collection_asset_count_is_live(smart_env):
    """asset_count on a smart collection reflects the current matching count."""
    client, api_key, library_id = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "Count Test",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Leica"},
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    # Initially 0
    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.json()["asset_count"] == 0

    # Ingest a matching asset
    _ingest_asset(
        client, api_key, library_id, "smart_leica.jpg",
        exif_data={"camera_make": "Leica", "taken_at": "2024-08-01T10:00:00+00:00"},
    )

    # Count should now be 1
    r3 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r3.json()["asset_count"] == 1


# ---------------------------------------------------------------------------
# Default type is "static"
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_default_collection_type_is_static(smart_env):
    """Collections created without type field default to static."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={"name": "Default Type"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    assert r.json()["type"] == "static"


# ---------------------------------------------------------------------------
# Smart collection with rating filters
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_smart_collection_e2e_browse_then_create(smart_env):
    """End-to-end: browse with filters, confirm results, create smart collection, verify it matches.

    This simulates the exact user flow:
    1. Ingest several assets with different cameras
    2. Browse with camera_make=Nikon filter → get N results
    3. Create smart collection from those same filters
    4. List collection assets → must return the same N results
    """
    client, api_key, library_id = smart_env

    # Ingest assets with different cameras
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

    # Step 1: Browse with camera_make=Nikon to confirm filter works
    r_browse = client.get(
        f"/v1/browse?library_id={library_id}&camera_make=Nikon",
        headers=_headers(api_key),
    )
    assert r_browse.status_code == 200
    browse_ids = [i["asset_id"] for i in r_browse.json()["items"]]
    assert a_nikon1 in browse_ids
    assert a_nikon2 in browse_ids
    assert a_canon not in browse_ids
    nikon_count = len([i for i in r_browse.json()["items"] if i["camera_make"] == "Nikon"])
    assert nikon_count >= 2

    # Step 2: Create smart collection with the SAME filters
    # This is what the Swift/web clients send
    r_create = client.post(
        "/v1/collections",
        json={
            "name": "E2E Nikon Collection",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Nikon"},
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    assert r_create.status_code == 201
    col_id = r_create.json()["collection_id"]

    # Step 3: List collection assets — must match browse results
    r_assets = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r_assets.status_code == 200
    col_ids = [i["asset_id"] for i in r_assets.json()["items"]]
    assert a_nikon1 in col_ids, f"Nikon1 missing from smart collection. Got: {col_ids}"
    assert a_nikon2 in col_ids, f"Nikon2 missing from smart collection. Got: {col_ids}"
    assert a_canon not in col_ids, "Canon should not be in Nikon smart collection"

    # Step 4: asset_count on the collection detail must match
    r_detail = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r_detail.status_code == 200
    assert r_detail.json()["asset_count"] >= 2


@pytest.mark.slow
def test_smart_collection_e2e_queryparams_style_filters(smart_env):
    """Test that filter keys matching BrowseFilter.queryParams work in saved_query.

    The Swift client's SaveSmartCollectionSheet builds filters from
    BrowseFilter.queryParams which uses keys like 'camera_make', 'star_min',
    'favorite'. These must round-trip through saved_query correctly.
    """
    client, api_key, library_id = smart_env

    # Ingest a favorited Canon asset
    a1 = _ingest_asset(
        client, api_key, library_id, "e2e_qp_match.jpg",
        exif_data={"camera_make": "Pentax", "taken_at": "2024-05-01T10:00:00+00:00"},
    )
    a2 = _ingest_asset(
        client, api_key, library_id, "e2e_qp_nomatch.jpg",
        exif_data={"camera_make": "Olympus", "taken_at": "2024-05-02T10:00:00+00:00"},
    )
    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))

    # Create smart collection using queryParams-style keys
    r = client.post(
        "/v1/collections",
        json={
            "name": "E2E QueryParams Style",
            "type": "smart",
            "saved_query": {
                "filters": {
                    "camera_make": "Pentax",
                    "favorite": True,
                },
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    col_id = r.json()["collection_id"]

    # Collection must contain the matching asset
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a1 in ids, f"Favorited Pentax asset missing. Got: {ids}"
    assert a2 not in ids, "Non-matching asset should not appear"


@pytest.mark.slow
def test_smart_collection_saved_query_is_visible(smart_env):
    """GET collection detail returns saved_query so the UI can display filters."""
    client, api_key, _ = smart_env

    r = client.post(
        "/v1/collections",
        json={
            "name": "Visible Filters",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Sony", "star_min": 4, "color": "red"},
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}", headers=_headers(api_key))
    assert r2.status_code == 200
    sq = r2.json()["saved_query"]
    assert sq is not None, "saved_query must be returned in collection detail"
    assert sq["filters"]["camera_make"] == "Sony"
    assert sq["filters"]["star_min"] == 4
    assert sq["filters"]["color"] == "red"


@pytest.mark.slow
def test_smart_collection_rating_filters(smart_env):
    """Smart collection with star + color filters returns matching assets."""
    client, api_key, library_id = smart_env

    a_match = _ingest_asset(client, api_key, library_id, "smart_rated_match.jpg",
        exif_data={"taken_at": "2024-09-01T10:00:00+00:00"})
    a_nomatch = _ingest_asset(client, api_key, library_id, "smart_rated_nomatch.jpg",
        exif_data={"taken_at": "2024-09-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_match}/rating", json={"stars": 5, "color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_nomatch}/rating", json={"stars": 2}, headers=_headers(api_key))

    r = client.post(
        "/v1/collections",
        json={
            "name": "Top Red Picks",
            "type": "smart",
            "saved_query": {
                "filters": {"star_min": 4, "color": "red"},
                "library_id": library_id,
            },
        },
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_match in ids
    assert a_nomatch not in ids
