"""End-to-end smart project tests that simulate exact client payloads.

These tests reproduce the exact JSON the web and Swift clients send
when creating smart projects using the filter algebra format:
  {"filters": [{"type": "camera_make", "value": "Canon"}, ...], "sort": "taken_at", "direction": "desc"}
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
from tests.machine_lineage import ingest_made


def _ingest_asset(client, api_key, library_id, rel_path, *, exif=None) -> str:
    from PIL import Image as PILImage
    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    data = {
        "lineage": ingest_made(),
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
    with PostgresContainer(PG_IMAGE) as cp:
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

        with PostgresContainer(PG_IMAGE) as tp:
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
def test_web_client_smart_project_payload(e2e_env):
    """Simulate the exact JSON the web client sends using the filter algebra.

    Web client builds a filter array from the active chiclets.
    """
    client, api_key, library_id = e2e_env

    a_canon = _ingest_asset(client, api_key, library_id, "web_canon.jpg",
                            exif={"camera_make": "Canon", "taken_at": "2024-06-01T10:00:00+00:00"})
    a_sony = _ingest_asset(client, api_key, library_id, "web_sony.jpg",
                           exif={"camera_make": "Sony", "taken_at": "2024-06-02T10:00:00+00:00"})

    web_payload = {
        "name": "Web Canon Test",
        "type": "smart",
        "saved_query": {
            "filters": [
                {"type": "camera_make", "value": "Canon"},
                {"type": "library", "value": library_id},
            ],
            "sort": "taken_at",
            "direction": "desc",
        },
    }

    r = client.post("/v1/projects", json=web_payload, headers=_h(api_key))
    assert r.status_code == 201, r.text
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_canon in ids, f"Canon asset not found. Got {len(ids)} items: {ids}"
    assert a_sony not in ids


@pytest.mark.slow
def test_swift_client_smart_project_payload(e2e_env):
    """Simulate the exact JSON the Swift client sends from SaveSmartProjectSheet.

    Swift QueryFilterState serializes as [LeafFilter] → filter algebra JSON.
    """
    client, api_key, library_id = e2e_env

    a_nikon = _ingest_asset(client, api_key, library_id, "swift_nikon.jpg",
                            exif={"camera_make": "Nikon", "taken_at": "2024-08-01T10:00:00+00:00"})
    a_other = _ingest_asset(client, api_key, library_id, "swift_other.jpg",
                            exif={"camera_make": "Panasonic", "taken_at": "2024-08-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_nikon}/rating",
               json={"favorite": True, "stars": 4},
               headers=_h(api_key))

    swift_payload = {
        "name": "Swift Nikon Favorites",
        "type": "smart",
        "saved_query": {
            "filters": [
                {"type": "camera_make", "value": "Nikon"},
                {"type": "favorite", "value": "yes"},
                {"type": "stars", "value": "4+"},
                {"type": "library", "value": library_id},
            ],
        },
    }

    r = client.post("/v1/projects", json=swift_payload, headers=_h(api_key))
    assert r.status_code == 201, r.text
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_nikon in ids, f"Nikon favorite not found. Items: {r2.json()['items']}"
    assert a_other not in ids

    r3 = client.get(f"/v1/projects/{col_id}", headers=_h(api_key))
    assert r3.status_code == 200
    sq = r3.json()["saved_query"]
    assert sq is not None
    filter_types = {f["type"] for f in sq["filters"]}
    assert "camera_make" in filter_types
    assert "favorite" in filter_types
    assert "stars" in filter_types
    assert r3.json()["asset_count"] >= 1


@pytest.mark.slow
def test_project_with_path_filter(e2e_env):
    """Smart project scoped to a library + path prefix."""
    client, api_key, library_id = e2e_env

    a_in = _ingest_asset(client, api_key, library_id, "2024/Travel/paris.jpg",
                         exif={"taken_at": "2024-03-01T10:00:00+00:00"})
    a_out = _ingest_asset(client, api_key, library_id, "2024/Home/garden.jpg",
                          exif={"taken_at": "2024-03-02T10:00:00+00:00"})

    r = client.post("/v1/projects", json={
        "name": "Travel Photos",
        "type": "smart",
        "saved_query": {
            "filters": [
                {"type": "path", "value": "2024/Travel"},
                {"type": "library", "value": library_id},
            ],
        },
    }, headers=_h(api_key))
    assert r.status_code == 201
    col_id = r.json()["project_id"]

    r2 = client.get(f"/v1/projects/{col_id}/assets", headers=_h(api_key))
    assert r2.status_code == 200
    ids = [i["asset_id"] for i in r2.json()["items"]]
    assert a_in in ids
    assert a_out not in ids
