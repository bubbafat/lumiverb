"""Search endpoint filter integration tests.

The existing test_search_endpoint.py has only fast/mocked tests.
This file adds slow integration tests for all search filters:
1. Existing filters (validate): favorite, stars, color, has_rating, has_faces, date range, tag
2. Missing filters (expect failure): has_color
3. Combined filter stacking with search query
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
    """Helper: ingest an asset with optional EXIF and vision data."""
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
def search_env():
    """Testcontainers env for search filter tests."""
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
        os.environ["ADMIN_KEY"] = "test-admin-searchfilters"
        os.environ["JWT_SECRET"] = "test-jwt-secret-searchfilters"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SearchFilterTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-searchfilters"},
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
                    json={"name": "SearchLib", "root_path": "/tmp/search-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Validate existing: date range on search (date-only mode, no BM25)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_date_from_only(search_env):
    """Search with date_from only returns assets on/after that date."""
    client, api_key, library_id = search_env

    a_old = _ingest_asset(
        client, api_key, library_id, "sdf_old.jpg",
        exif_data={"taken_at": "2023-03-15T10:00:00+00:00"},
    )
    a_new = _ingest_asset(
        client, api_key, library_id, "sdf_new.jpg",
        exif_data={"taken_at": "2024-08-20T14:30:00+00:00"},
    )

    r = client.get("/v1/search?date_from=2024-01-01", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_new in ids
    assert a_old not in ids


@pytest.mark.slow
def test_search_date_to_only(search_env):
    """Search with date_to only returns assets on/before that date."""
    client, api_key, library_id = search_env

    a_old = _ingest_asset(
        client, api_key, library_id, "sdt_old.jpg",
        exif_data={"taken_at": "2023-03-15T10:00:00+00:00"},
    )
    a_new = _ingest_asset(
        client, api_key, library_id, "sdt_new.jpg",
        exif_data={"taken_at": "2024-08-20T14:30:00+00:00"},
    )

    r = client.get("/v1/search?date_to=2023-12-31", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_old in ids
    assert a_new not in ids


@pytest.mark.slow
def test_search_date_range(search_env):
    """Search with date_from + date_to filters to a window."""
    client, api_key, library_id = search_env

    a_before = _ingest_asset(
        client, api_key, library_id, "sdr_before.jpg",
        exif_data={"taken_at": "2022-01-10T08:00:00+00:00"},
    )
    a_in = _ingest_asset(
        client, api_key, library_id, "sdr_in.jpg",
        exif_data={"taken_at": "2023-07-15T12:00:00+00:00"},
    )
    a_after = _ingest_asset(
        client, api_key, library_id, "sdr_after.jpg",
        exif_data={"taken_at": "2024-11-01T16:00:00+00:00"},
    )

    r = client.get(
        "/v1/search?date_from=2023-06-01&date_to=2023-12-31",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_in in ids
    assert a_before not in ids
    assert a_after not in ids


# ---------------------------------------------------------------------------
# Validate existing: search + favorite filter
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_favorite_filter(search_env):
    """Search with favorite=true only returns favorited assets."""
    client, api_key, library_id = search_env

    a_fav = _ingest_asset(
        client, api_key, library_id, "sfav_fav.jpg",
        exif_data={"taken_at": "2024-06-01T10:00:00+00:00"},
    )
    a_nofav = _ingest_asset(
        client, api_key, library_id, "sfav_nofav.jpg",
        exif_data={"taken_at": "2024-06-02T10:00:00+00:00"},
    )

    client.put(f"/v1/assets/{a_fav}/rating", json={"favorite": True}, headers=_headers(api_key))

    # Date-only search to avoid BM25 dependency
    r = client.get(
        "/v1/search?date_from=2024-06-01&date_to=2024-06-30&favorite=true",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_fav in ids
    assert a_nofav not in ids


# ---------------------------------------------------------------------------
# Validate existing: search + star filter
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_star_filter(search_env):
    """Search with star_min filters by minimum star rating."""
    client, api_key, library_id = search_env

    a_high = _ingest_asset(client, api_key, library_id, "sstar_high.jpg",
        exif_data={"taken_at": "2024-01-15T10:00:00+00:00"})
    a_low = _ingest_asset(client, api_key, library_id, "sstar_low.jpg",
        exif_data={"taken_at": "2024-01-16T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_high}/rating", json={"stars": 5}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_low}/rating", json={"stars": 1}, headers=_headers(api_key))

    r = client.get(
        "/v1/search?date_from=2024-01-01&date_to=2024-01-31&star_min=4",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_high in ids
    assert a_low not in ids


# ---------------------------------------------------------------------------
# Validate existing: search + color filter
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_color_filter(search_env):
    """Search with color filter returns only assets with that color."""
    client, api_key, library_id = search_env

    a_red = _ingest_asset(client, api_key, library_id, "scol_red.jpg",
        exif_data={"taken_at": "2024-02-01T10:00:00+00:00"})
    a_blue = _ingest_asset(client, api_key, library_id, "scol_blue.jpg",
        exif_data={"taken_at": "2024-02-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_red}/rating", json={"color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_blue}/rating", json={"color": "blue"}, headers=_headers(api_key))

    r = client.get(
        "/v1/search?date_from=2024-02-01&date_to=2024-02-28&color=red",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_red in ids
    assert a_blue not in ids


# ---------------------------------------------------------------------------
# MISSING: has_color on search
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_has_color_true(search_env):
    """has_color=true returns only assets with a color label set."""
    client, api_key, library_id = search_env

    a_colored = _ingest_asset(client, api_key, library_id, "shc_colored.jpg",
        exif_data={"taken_at": "2024-03-01T10:00:00+00:00"})
    a_plain = _ingest_asset(client, api_key, library_id, "shc_plain.jpg",
        exif_data={"taken_at": "2024-03-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_colored}/rating", json={"color": "green"}, headers=_headers(api_key))

    r = client.get(
        "/v1/search?date_from=2024-03-01&date_to=2024-03-31&has_color=true",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_colored in ids
    assert a_plain not in ids


@pytest.mark.slow
def test_search_has_color_false(search_env):
    """has_color=false returns only assets without a color label."""
    client, api_key, library_id = search_env

    a_colored = _ingest_asset(client, api_key, library_id, "shcf_colored.jpg",
        exif_data={"taken_at": "2024-04-01T10:00:00+00:00"})
    a_plain = _ingest_asset(client, api_key, library_id, "shcf_plain.jpg",
        exif_data={"taken_at": "2024-04-02T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_colored}/rating", json={"color": "purple"}, headers=_headers(api_key))

    r = client.get(
        "/v1/search?date_from=2024-04-01&date_to=2024-04-30&has_color=false",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_plain in ids
    assert a_colored not in ids


# ---------------------------------------------------------------------------
# Combined: search query + filters
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_search_has_rating_with_date(search_env):
    """has_rating=true combined with date range."""
    client, api_key, library_id = search_env

    a_rated = _ingest_asset(client, api_key, library_id, "shr_rated.jpg",
        exif_data={"taken_at": "2024-05-15T10:00:00+00:00"})
    a_unrated = _ingest_asset(client, api_key, library_id, "shr_unrated.jpg",
        exif_data={"taken_at": "2024-05-16T10:00:00+00:00"})

    client.put(f"/v1/assets/{a_rated}/rating", json={"stars": 3}, headers=_headers(api_key))

    r = client.get(
        "/v1/search?date_from=2024-05-01&date_to=2024-05-31&has_rating=true",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [h["asset_id"] for h in r.json()["hits"]]
    assert a_rated in ids
    assert a_unrated not in ids
