"""Filtered facets tests — facet aggregation respects active filters.

Tests that GET /v1/assets/facets with ?f=prefix:value params returns
aggregated values scoped to the filtered result set.

Uses testcontainers Postgres with real migrations.
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import MagicMock, patch

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
        "media_type": media_type,
        "width": "100",
        "height": "100",
    }
    if exif_data is not None:
        data["exif"] = json.dumps(exif_data)

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
def facet_env():
    """Testcontainers env with pre-seeded assets for facet tests."""
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
        os.environ["ADMIN_KEY"] = "test-admin-facets"
        os.environ["JWT_SECRET"] = "test-jwt-secret-facets"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "FacetTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-facets"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

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
                    json={"name": "FacetLib", "root_path": "/tmp/facet-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                # Ingest diverse assets
                canon1 = _ingest_asset(
                    client, api_key, library_id, "canon/photo1.jpg",
                    exif_data={"camera_make": "Canon", "camera_model": "EOS R5", "iso": 200},
                )
                canon2 = _ingest_asset(
                    client, api_key, library_id, "canon/photo2.jpg",
                    exif_data={"camera_make": "Canon", "camera_model": "EOS R6", "iso": 400},
                )
                sony1 = _ingest_asset(
                    client, api_key, library_id, "sony/photo1.jpg",
                    exif_data={"camera_make": "Sony", "camera_model": "A7IV", "iso": 800},
                )
                sony_vid = _ingest_asset(
                    client, api_key, library_id, "sony/video1.mp4",
                    media_type="video",
                    exif_data={"camera_make": "Sony", "camera_model": "A7IV"},
                )

                yield {
                    "client": client,
                    "api_key": api_key,
                    "library_id": library_id,
                    "canon1": canon1,
                    "canon2": canon2,
                    "sony1": sony1,
                    "sony_vid": sony_vid,
                }

        _engines.clear()


# ---------------------------------------------------------------------------
# No filters — full facets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_no_filters(facet_env):
    """Facets with no filters return aggregates across all assets."""
    e = facet_env
    r = e["client"].get(
        f"/v1/assets/facets?f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    assert "Canon" in data["camera_makes"]
    assert "Sony" in data["camera_makes"]
    assert "image" in data["media_types"]
    assert "video" in data["media_types"]


# ---------------------------------------------------------------------------
# Camera make filter narrows facets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_camera_make_filter(facet_env):
    """Facets with camera_make:Canon only show Canon models."""
    e = facet_env
    r = e["client"].get(
        f"/v1/assets/facets?f=camera_make:Canon&f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    # Only Canon camera makes in the filtered set
    assert data["camera_makes"] == ["Canon"]
    # Only Canon models
    assert "EOS R5" in data["camera_models"]
    assert "EOS R6" in data["camera_models"]
    assert "A7IV" not in data["camera_models"]


# ---------------------------------------------------------------------------
# Media type filter narrows facets
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_media_type_filter(facet_env):
    """Facets with media:image exclude video assets from aggregation."""
    e = facet_env
    r = e["client"].get(
        f"/v1/assets/facets?f=media:image&f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    # Only images
    assert data["media_types"] == ["image"]
    # But cameras from both Canon and Sony images
    assert "Canon" in data["camera_makes"]
    assert "Sony" in data["camera_makes"]


# ---------------------------------------------------------------------------
# Multiple filters AND together
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_multiple_filters(facet_env):
    """Multiple filters AND together in facet aggregation."""
    e = facet_env
    r = e["client"].get(
        f"/v1/assets/facets?f=camera_make:Sony&f=media:image&f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    # Only Sony images
    assert data["camera_makes"] == ["Sony"]
    assert data["media_types"] == ["image"]
    # ISO from Sony image only (800)
    assert data["iso_range"][0] == 800
    assert data["iso_range"][1] == 800


# ---------------------------------------------------------------------------
# ISO range filter
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_iso_range_filter(facet_env):
    """ISO range filter narrows facet aggregation."""
    e = facet_env
    r = e["client"].get(
        f"/v1/assets/facets?f=iso:100-500&f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    # Only Canon assets have ISO 200/400 in range 100-500
    assert data["camera_makes"] == ["Canon"]
    assert data["iso_range"][0] == 200
    assert data["iso_range"][1] == 400


# ---------------------------------------------------------------------------
# Rating filter requires JOIN
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_with_favorite_filter(facet_env):
    """Facets with favorite filter triggers rating JOIN and narrows results."""
    e = facet_env
    # Rate canon1 as favorite
    e["client"].put(
        f"/v1/assets/{e['canon1']}/rating",
        json={"favorite": True, "stars": 5},
        headers=_headers(e["api_key"]),
    )

    r = e["client"].get(
        f"/v1/assets/facets?f=favorite:yes&f=library:{e['library_id']}",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    data = r.json()
    # Only canon1 is favorited
    assert data["camera_makes"] == ["Canon"]
    assert data["camera_models"] == ["EOS R5"]
    assert data["iso_range"] == [200, 200]


# ---------------------------------------------------------------------------
# Text search with mocked Quickwit
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_facets_with_text_search(facet_env):
    """Facets with text search scope aggregation to Quickwit candidates."""
    e = facet_env

    mock_qw = MagicMock()
    mock_qw.enabled = True
    # Quickwit returns only Sony assets as search candidates
    mock_qw.search_tenant.return_value = [
        {"asset_id": e["sony1"], "score": 0.9},
        {"asset_id": e["sony_vid"], "score": 0.8},
    ]
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get(
            f"/v1/assets/facets?f=query:sunset&f=library:{e['library_id']}",
            headers=_headers(e["api_key"]),
        )
    assert r.status_code == 200
    data = r.json()
    # Only Sony assets are in the candidate set
    assert data["camera_makes"] == ["Sony"]
    assert "Canon" not in data["camera_makes"]


@pytest.mark.slow
def test_facets_text_search_no_results(facet_env):
    """Facets with text search returning no candidates yields empty facets."""
    e = facet_env

    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = []
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get(
            f"/v1/assets/facets?f=query:nonexistent&f=library:{e['library_id']}",
            headers=_headers(e["api_key"]),
        )
    assert r.status_code == 200
    data = r.json()
    assert data["camera_makes"] == []
    assert data["media_types"] == []
    assert data["iso_range"] == [None, None]
