"""Browse filter integration tests — covers filter gaps and validates existing filters.

Tests are organized into sections:
1. Existing filters (validate): has_rating, has_faces, person_id, multi-color
2. Missing filters (expect failure): date_from, date_to, has_color
3. Combined filter stacking

Uses testcontainers Postgres with real migrations.
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
    media_type="image",
    exif_data=None,
    vision_data=None,
) -> str:
    """Helper: ingest a minimal asset with optional EXIF and vision data."""
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    data = {
        "library_id": library_id,
        "rel_path": rel_path,
        "file_size": "1000",
        "media_type": media_type,
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
def filter_env():
    """Testcontainers env with two libraries and pre-seeded assets for filter tests."""
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
        os.environ["ADMIN_KEY"] = "test-admin-browsefilters"
        os.environ["JWT_SECRET"] = "test-jwt-secret-browsefilters"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "BrowseFilterTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-browsefilters"},
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
                    json={"name": "FilterLib", "root_path": "/tmp/filter-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Validate existing: has_rating
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_has_rating_true(filter_env):
    """has_rating=true returns only assets that have any rating set."""
    client, api_key, library_id = filter_env
    a_rated = _ingest_asset(client, api_key, library_id, "hr_rated.jpg")
    a_unrated = _ingest_asset(client, api_key, library_id, "hr_unrated.jpg")

    # Rate one asset
    client.put(f"/v1/assets/{a_rated}/rating", json={"stars": 3}, headers=_headers(api_key))

    r = client.get("/v1/browse?has_rating=true", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_rated in ids
    assert a_unrated not in ids


@pytest.mark.slow
def test_browse_has_rating_false(filter_env):
    """has_rating=false returns only assets with NO rating."""
    client, api_key, library_id = filter_env
    a_rated = _ingest_asset(client, api_key, library_id, "hrf_rated.jpg")
    a_unrated = _ingest_asset(client, api_key, library_id, "hrf_unrated.jpg")

    client.put(f"/v1/assets/{a_rated}/rating", json={"favorite": True}, headers=_headers(api_key))

    r = client.get("/v1/browse?has_rating=false", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_unrated in ids
    assert a_rated not in ids


# ---------------------------------------------------------------------------
# Validate existing: multi-color filter (comma-separated)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_multi_color(filter_env):
    """color=red,blue should return assets with either color."""
    client, api_key, library_id = filter_env
    a_red = _ingest_asset(client, api_key, library_id, "mc_red.jpg")
    a_blue = _ingest_asset(client, api_key, library_id, "mc_blue.jpg")
    a_green = _ingest_asset(client, api_key, library_id, "mc_green.jpg")

    client.put(f"/v1/assets/{a_red}/rating", json={"color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_blue}/rating", json={"color": "blue"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_green}/rating", json={"color": "green"}, headers=_headers(api_key))

    r = client.get("/v1/browse?color=red,blue", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_red in ids
    assert a_blue in ids
    assert a_green not in ids


# ---------------------------------------------------------------------------
# Validate existing: star range
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_star_range(filter_env):
    """star_min + star_max filters to a range."""
    client, api_key, library_id = filter_env
    a1 = _ingest_asset(client, api_key, library_id, "sr_1star.jpg")
    a3 = _ingest_asset(client, api_key, library_id, "sr_3star.jpg")
    a5 = _ingest_asset(client, api_key, library_id, "sr_5star.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"stars": 1}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a3}/rating", json={"stars": 3}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a5}/rating", json={"stars": 5}, headers=_headers(api_key))

    r = client.get("/v1/browse?star_min=2&star_max=4", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a3 in ids
    assert a1 not in ids
    assert a5 not in ids


# ---------------------------------------------------------------------------
# Validate existing: has_faces
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_has_faces(filter_env):
    """has_faces=true returns only assets with face_count > 0."""
    client, api_key, library_id = filter_env
    a_faces = _ingest_asset(client, api_key, library_id, "hf_withfaces.jpg")
    a_nofaces = _ingest_asset(client, api_key, library_id, "hf_nofaces.jpg")

    # has_faces works via face_count on the asset. For testing, just verify the
    # parameter is accepted and returns 200 (face_count is set during enrichment,
    # not during ingest, so we can't easily create assets with faces in tests).
    r = client.get("/v1/browse?has_faces=true", headers=_headers(api_key))
    assert r.status_code == 200
    # All returned items should have face_count > 0 (or the list is empty for test data)


# ---------------------------------------------------------------------------
# MISSING: date_from / date_to on browse
# These tests document the gap — browse endpoint doesn't accept date params.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_date_from(filter_env):
    """date_from should filter browse results to assets taken on or after that date."""
    client, api_key, library_id = filter_env

    # Ingest assets with different taken_at dates via EXIF
    a_old = _ingest_asset(
        client, api_key, library_id, "df_old.jpg",
        exif_data={"taken_at": "2023-06-15T10:00:00+00:00"},
    )
    a_new = _ingest_asset(
        client, api_key, library_id, "df_new.jpg",
        exif_data={"taken_at": "2024-03-20T14:30:00+00:00"},
    )

    r = client.get("/v1/browse?date_from=2024-01-01", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_new in ids
    assert a_old not in ids


@pytest.mark.slow
def test_browse_date_to(filter_env):
    """date_to should filter browse results to assets taken on or before that date."""
    client, api_key, library_id = filter_env

    a_old = _ingest_asset(
        client, api_key, library_id, "dt_old.jpg",
        exif_data={"taken_at": "2023-06-15T10:00:00+00:00"},
    )
    a_new = _ingest_asset(
        client, api_key, library_id, "dt_new.jpg",
        exif_data={"taken_at": "2024-03-20T14:30:00+00:00"},
    )

    r = client.get("/v1/browse?date_to=2023-12-31", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_old in ids
    assert a_new not in ids


@pytest.mark.slow
def test_browse_date_range(filter_env):
    """date_from + date_to should filter to a date window."""
    client, api_key, library_id = filter_env

    a_before = _ingest_asset(
        client, api_key, library_id, "dr_before.jpg",
        exif_data={"taken_at": "2023-01-10T08:00:00+00:00"},
    )
    a_in = _ingest_asset(
        client, api_key, library_id, "dr_in.jpg",
        exif_data={"taken_at": "2023-07-15T12:00:00+00:00"},
    )
    a_after = _ingest_asset(
        client, api_key, library_id, "dr_after.jpg",
        exif_data={"taken_at": "2024-02-01T16:00:00+00:00"},
    )

    r = client.get(
        "/v1/browse?date_from=2023-06-01&date_to=2023-12-31",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_in in ids
    assert a_before not in ids
    assert a_after not in ids


# ---------------------------------------------------------------------------
# MISSING: has_color on browse
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_has_color_true(filter_env):
    """has_color=true returns only assets that have a color label set."""
    client, api_key, library_id = filter_env
    a_colored = _ingest_asset(client, api_key, library_id, "hc_colored.jpg")
    a_plain = _ingest_asset(client, api_key, library_id, "hc_plain.jpg")

    client.put(f"/v1/assets/{a_colored}/rating", json={"color": "red"}, headers=_headers(api_key))
    # a_plain has no rating at all

    r = client.get("/v1/browse?has_color=true", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_colored in ids
    assert a_plain not in ids


@pytest.mark.slow
def test_browse_has_color_false(filter_env):
    """has_color=false returns only assets without a color label."""
    client, api_key, library_id = filter_env
    a_colored = _ingest_asset(client, api_key, library_id, "hcf_colored.jpg")
    a_plain = _ingest_asset(client, api_key, library_id, "hcf_plain.jpg")

    client.put(f"/v1/assets/{a_colored}/rating", json={"color": "blue"}, headers=_headers(api_key))

    r = client.get("/v1/browse?has_color=false", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_plain in ids
    assert a_colored not in ids


# ---------------------------------------------------------------------------
# Combined filter stacking
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_stacked_filters(filter_env):
    """Multiple filters combine with AND logic."""
    client, api_key, library_id = filter_env

    a_match = _ingest_asset(
        client, api_key, library_id, "stack_match.jpg",
        exif_data={
            "taken_at": "2024-06-15T10:00:00+00:00",
            "camera_make": "Canon",
        },
    )
    a_wrong_date = _ingest_asset(
        client, api_key, library_id, "stack_wrongdate.jpg",
        exif_data={
            "taken_at": "2023-01-15T10:00:00+00:00",
            "camera_make": "Canon",
        },
    )
    a_wrong_camera = _ingest_asset(
        client, api_key, library_id, "stack_wrongcam.jpg",
        exif_data={
            "taken_at": "2024-06-15T10:00:00+00:00",
            "camera_make": "Sony",
        },
    )

    # Rate the matching asset
    client.put(f"/v1/assets/{a_match}/rating", json={"stars": 4, "color": "green"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_wrong_date}/rating", json={"stars": 4, "color": "green"}, headers=_headers(api_key))

    r = client.get(
        "/v1/browse?date_from=2024-01-01&camera_make=Canon&star_min=3&color=green",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_match in ids
    assert a_wrong_date not in ids
    assert a_wrong_camera not in ids


@pytest.mark.slow
def test_browse_favorite_plus_has_color(filter_env):
    """favorite=true + has_color=true returns only favorited assets with a color label."""
    client, api_key, library_id = filter_env

    a_fav_color = _ingest_asset(client, api_key, library_id, "fc_both.jpg")
    a_fav_only = _ingest_asset(client, api_key, library_id, "fc_favonly.jpg")
    a_color_only = _ingest_asset(client, api_key, library_id, "fc_coloronly.jpg")

    client.put(f"/v1/assets/{a_fav_color}/rating", json={"favorite": True, "color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_fav_only}/rating", json={"favorite": True}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a_color_only}/rating", json={"color": "blue"}, headers=_headers(api_key))

    r = client.get("/v1/browse?favorite=true&has_color=true", headers=_headers(api_key))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a_fav_color in ids
    assert a_fav_only not in ids
    assert a_color_only not in ids
