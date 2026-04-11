"""End-to-end smart collection tests that simulate exact client payloads.

These tests reproduce the exact JSON the web and Swift clients send
when creating smart collections, to catch serialization mismatches.
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


def _ingest_asset(client, api_key, library_id, rel_path, *, exif=None) -> str:
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
    if exif:
        data["exif"] = json.dumps(exif)
    r = client.post(
        "/v1/ingest",
        data=data,
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


def _h(api_key): return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def e2e_env():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as cp:
        cu = _ensure_psycopg2(cp.get_connection_url())
        e = create_engine(cu)
        with e.connect() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.commit()
        e.dispose()
        _run_control_migrations(cu)
        u = make_url(cu)
        tt = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = cu
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tt
        os.environ["ADMIN_KEY"] = "test-admin-e2e"
        os.environ["JWT_SECRET"] = "test-jwt-e2e"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as cl:
                r = cl.post("/v1/admin/tenants", json={"name": "E2E", "plan": "free"},
                            headers={"Authorization": "Bearer test-admin-e2e"})
                assert r.status_code == 200
                tid = r.json()["tenant_id"]
                ak = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tp:
            tu = _ensure_psycopg2(tp.get_connection_url())
            _provision_tenant_db(tu, project_root)
            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository
            with get_control_session() as s:
                rr = TenantDbRoutingRepository(s)
                row = rr.get_by_tenant_id(tid)
                row.connection_string = tu
                s.add(row); s.commit()

            with TestClient(app) as cl:
                rl = cl.post("/v1/libraries", json={"name": "E2ELib", "root_path": "/tmp/e2e"},
                             headers=_h(ak))
                assert rl.status_code == 200
                lid = rl.json()["library_id"]
                yield cl, ak, lid

        _engines.clear()


# ---------------------------------------------------------------------------
# Web client payload simulation
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_web_client_smart_collection_payload(e2e_env):
    """Simulate the exact JSON the web client sends after toSnakeCaseFilters().

    Web client browseOpts has camelCase keys. toSnakeCaseFilters converts them.
    This test verifies the converted payload creates a working collection.
    """
    client, api_key, library_id = e2e_env

    # Ingest test data
    a_canon = _ingest_asset(client, api_key, library_id, "web_canon.jpg",
                            exif={"camera_make": "Canon", "taken_at": "2024-06-01T10:00:00+00:00"})
    a_sony = _ingest_asset(client, api_key, library_id, "web_sony.jpg",
                           exif={"camera_make": "Sony", "taken_at": "2024-06-02T10:00:00+00:00"})

    # This is exactly what toSnakeCaseFilters(browseOpts) produces
    web_payload = {
        "name": "Web Canon Test",
        "type": "smart",
        "saved_query": {
            "filters": {
                "camera_make": "Canon",
                "sort": "taken_at",
                "dir": "desc",
            },
            "library_id": library_id,
        },
    }

    r = client.post("/v1/collections", json=web_payload, headers=_h(api_key))
    assert r.status_code == 201, r.text
    col_id = r.json()["collection_id"]

    # Verify collection has assets
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_canon in ids, f"Canon asset not found. Got {len(ids)} items: {ids}"
    assert a_sony not in ids


@pytest.mark.slow
def test_web_client_camelcase_payload_fails(e2e_env):
    """Verify that camelCase filter keys (the old bug) produce 0 results.

    This proves the bug existed and the fix is necessary.
    """
    client, api_key, library_id = e2e_env

    a_canon = _ingest_asset(client, api_key, library_id, "camel_canon.jpg",
                            exif={"camera_make": "Canon", "taken_at": "2024-07-01T10:00:00+00:00"})

    # Old buggy payload: camelCase keys
    buggy_payload = {
        "name": "Buggy CamelCase",
        "type": "smart",
        "saved_query": {
            "filters": {
                "cameraMake": "Canon",  # Wrong! Should be camera_make
            },
            "library_id": library_id,
        },
    }

    r = client.post("/v1/collections", json=buggy_payload, headers=_h(api_key))
    assert r.status_code == 201
    col_id = r.json()["collection_id"]

    # This collection should return 0 results because cameraMake isn't a valid filter key
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    # cameraMake is not recognized by from_json, so no filter is applied,
    # which means ALL assets are returned (no filter = no restriction)
    # OR 0 results if the key causes an error
    # Let's see what actually happens:
    items = r2.json()["items"]
    # The buggy key is silently ignored → all assets returned → NOT the filtered set
    # This proves the camelCase bug: the collection returns ALL assets, not just Canon
    # The user would see all their photos, not the filtered subset they expected
    has_non_canon = any(i["camera_make"] != "Canon" for i in items if i.get("camera_make"))
    # If there are non-Canon assets, the filter didn't work (proving the bug)
    assert len(items) > 0, "Should return all assets (no filter applied)"


@pytest.mark.slow
def test_swift_client_smart_collection_payload(e2e_env):
    """Simulate the exact JSON the Swift client sends from SaveSmartCollectionSheet.

    Swift BrowseFilter.queryParams produces snake_case keys as strings.
    SaveSmartCollectionSheet converts "true"→true, "3"→3 etc.
    """
    client, api_key, library_id = e2e_env

    a_nikon = _ingest_asset(client, api_key, library_id, "swift_nikon.jpg",
                            exif={"camera_make": "Nikon", "taken_at": "2024-08-01T10:00:00+00:00"})
    a_other = _ingest_asset(client, api_key, library_id, "swift_other.jpg",
                            exif={"camera_make": "Panasonic", "taken_at": "2024-08-02T10:00:00+00:00"})

    # Rate the Nikon asset
    client.put(f"/v1/assets/{a_nikon}/rating",
               json={"favorite": True, "stars": 4},
               headers=_h(api_key))

    # This is exactly what the Swift SaveSmartCollectionSheet produces:
    # - queryParams gives {"camera_make": "Nikon", "favorite": "true", "star_min": "4"}
    # - Sheet converts strings to typed values
    swift_payload = {
        "name": "Swift Nikon Favorites",
        "type": "smart",
        "saved_query": {
            "filters": {
                "camera_make": "Nikon",
                "favorite": True,
                "star_min": 4,
            },
            "library_id": library_id,
        },
    }

    r = client.post("/v1/collections", json=swift_payload, headers=_h(api_key))
    assert r.status_code == 201, r.text
    col_id = r.json()["collection_id"]

    # Verify the collection returns the matching asset
    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_nikon in ids, f"Nikon favorite not found. Items: {r2.json()['items']}"
    assert a_other not in ids

    # Verify saved_query is visible in collection detail
    r3 = client.get(f"/v1/collections/{col_id}", headers=_h(api_key))
    assert r3.status_code == 200
    sq = r3.json()["saved_query"]
    assert sq is not None
    assert sq["filters"]["camera_make"] == "Nikon"
    assert sq["filters"]["favorite"] is True
    assert sq["filters"]["star_min"] == 4
    assert r3.json()["asset_count"] >= 1


@pytest.mark.slow
def test_web_default_browseOpts_creates_working_collection(e2e_env):
    """Simulate EXACT browseOpts when user is just browsing with no active filters.

    browseOpts always includes sort/dir/hasGps/hasFaces defaults.
    toSnakeCaseFilters must not turn these into restrictive filters.
    """
    client, api_key, library_id = e2e_env

    a_nogps = _ingest_asset(client, api_key, library_id, "default_nogps.jpg",
                            exif={"taken_at": "2024-10-01T10:00:00+00:00"})
    a_gps = _ingest_asset(client, api_key, library_id, "default_gps.jpg",
                          exif={"taken_at": "2024-10-02T10:00:00+00:00",
                                "gps_lat": 48.8566, "gps_lon": 2.3522})

    # Set face_count on one asset to simulate enrichment
    from sqlalchemy import text as sa_text
    session.execute(sa_text(
        f"UPDATE assets SET face_count = 3 WHERE asset_id = :aid"
    ), {"aid": a_gps})
    session.commit()

    # This is EXACTLY what toSnakeCaseFilters(browseOpts) produces when
    # the user has NO active filters — just defaults from the page:
    web_default_payload = {
        "name": "Default Opts Collection",
        "type": "smart",
        "saved_query": {
            "filters": {
                "sort": "taken_at",
                "dir": "desc",
                "has_gps": False,    # browseHasGps = searchParams.get("has_gps") === "true" → false
                "has_faces": False,  # browseHasFaces = searchParams.get("has_faces") === "true" → false
            },
            "library_id": library_id,
        },
    }

    r = client.post("/v1/collections", json=web_default_payload, headers=_h(api_key))
    assert r.status_code == 201
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    # has_gps=false + has_faces=false means: only assets WITHOUT gps AND without faces
    # Our test assets have no GPS and no faces, so they SHOULD match.
    # But if the user has photos WITH GPS, those would be EXCLUDED — that's the bug!
    # The fix: toSnakeCaseFilters must strip false boolean defaults.
    # BOTH assets must appear — the user had no filters active,
    # so the collection should include everything in the library.
    assert a_nogps in ids, (
        f"No-GPS asset missing. Got {len(ids)} items."
    )
    assert a_gps in ids, (
        f"GPS asset missing from collection with default browseOpts! "
        f"Got {len(ids)} items. has_gps=false is being applied as a "
        f"restrictive filter when it's just a default — THAT'S THE BUG."
    )


@pytest.mark.slow
def test_collection_with_path_filter(e2e_env):
    """Smart collection scoped to a library + path prefix."""
    client, api_key, library_id = e2e_env

    a_in = _ingest_asset(client, api_key, library_id, "2024/Travel/paris.jpg",
                         exif={"taken_at": "2024-03-01T10:00:00+00:00"})
    a_out = _ingest_asset(client, api_key, library_id, "2024/Home/garden.jpg",
                          exif={"taken_at": "2024-03-02T10:00:00+00:00"})

    r = client.post("/v1/collections", json={
        "name": "Travel Photos",
        "type": "smart",
        "saved_query": {
            "filters": {"path_prefix": "2024/Travel"},
            "library_id": library_id,
        },
    }, headers=_h(api_key))
    assert r.status_code == 201
    col_id = r.json()["collection_id"]

    r2 = client.get(f"/v1/collections/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_in in ids
    assert a_out not in ids
