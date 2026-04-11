"""Integration tests for the unified query endpoint (GET /v1/query).

Tests:
  - Query with no filters returns all assets
  - Query with structured filters narrows correctly
  - Multiple filters AND together
  - Keyset cursor pagination
  - Candidate-set pattern with mocked Quickwit
  - Search context annotation on results
  - Empty result set
  - Capabilities endpoint returns all filters

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
def query_env():
    """Testcontainers env with a library and pre-seeded assets."""
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
        os.environ["ADMIN_KEY"] = "test-admin-unified-query"
        os.environ["JWT_SECRET"] = "test-jwt-secret-unified-query"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "QueryTestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-unified-query"},
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
                    json={"name": "QueryLib", "root_path": "/tmp/query-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                # Ingest test assets
                canon_photo = _ingest_asset(
                    client, api_key, library_id, "canon/photo1.jpg",
                    exif_data={"camera_make": "Canon", "camera_model": "EOS R5", "iso": 200},
                )
                sony_photo = _ingest_asset(
                    client, api_key, library_id, "sony/photo2.jpg",
                    exif_data={"camera_make": "Sony", "camera_model": "A7IV", "iso": 800},
                )
                sony_video = _ingest_asset(
                    client, api_key, library_id, "sony/video1.mp4",
                    media_type="video",
                    exif_data={"camera_make": "Sony", "camera_model": "A7IV"},
                )
                iphone_photo = _ingest_asset(
                    client, api_key, library_id, "iphone/photo3.jpg",
                    exif_data={"camera_make": "Apple", "camera_model": "iPhone 15 Pro", "iso": 50},
                )
                # Asset with vision metadata for text search testing
                described_photo = _ingest_asset(
                    client, api_key, library_id, "described/woman_portrait.jpg",
                    exif_data={"camera_make": "Canon", "camera_model": "EOS R5"},
                    vision_data={
                        "model_id": "test-vision",
                        "description": "A woman standing in a sunlit garden",
                        "tags": ["woman", "portrait", "garden", "sunlight"],
                    },
                )

                # Rate some assets
                client.put(f"/v1/assets/{canon_photo}/rating", json={"stars": 5, "favorite": True}, headers=_headers(api_key))
                client.put(f"/v1/assets/{sony_photo}/rating", json={"stars": 3}, headers=_headers(api_key))

                yield {
                    "client": client,
                    "api_key": api_key,
                    "library_id": library_id,
                    "canon_photo": canon_photo,
                    "sony_photo": sony_photo,
                    "sony_video": sony_video,
                    "iphone_photo": iphone_photo,
                    "described_photo": described_photo,
                }

        _engines.clear()


# ---------------------------------------------------------------------------
# Basic query (no filters)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_no_filters_returns_all(query_env):
    """Query with no filters returns all active assets."""
    e = query_env
    r = e["client"].get("/v1/query", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    data = r.json()
    ids = [i["asset_id"] for i in data["items"]]
    assert e["canon_photo"] in ids
    assert e["sony_photo"] in ids
    assert e["sony_video"] in ids
    assert e["iphone_photo"] in ids


# ---------------------------------------------------------------------------
# Camera filter
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_camera_make(query_env):
    """Filter by camera_make returns only matching assets."""
    e = query_env
    r = e["client"].get("/v1/query?f=camera_make:Sony", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["sony_photo"] in ids
    assert e["sony_video"] in ids
    assert e["canon_photo"] not in ids
    assert e["iphone_photo"] not in ids


# ---------------------------------------------------------------------------
# Media type filter
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_media_type(query_env):
    """Filter by media type returns only matching assets."""
    e = query_env
    r = e["client"].get("/v1/query?f=media:video", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["sony_video"] in ids
    assert e["canon_photo"] not in ids


# ---------------------------------------------------------------------------
# Multiple filters AND
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_multiple_filters_and(query_env):
    """Multiple filters stack as AND."""
    e = query_env
    r = e["client"].get(
        "/v1/query?f=camera_make:Sony&f=media:image",
        headers=_headers(e["api_key"]),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["sony_photo"] in ids
    # Sony video excluded by media:image
    assert e["sony_video"] not in ids
    # Canon excluded by camera_make:Sony
    assert e["canon_photo"] not in ids


# ---------------------------------------------------------------------------
# ISO range filter
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_iso_range(query_env):
    """ISO range filter narrows results."""
    e = query_env
    r = e["client"].get("/v1/query?f=iso:100-500", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["canon_photo"] in ids  # iso 200
    assert e["sony_photo"] not in ids  # iso 800


# ---------------------------------------------------------------------------
# Rating filters
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_favorite(query_env):
    """Favorite filter returns only favorited assets."""
    e = query_env
    r = e["client"].get("/v1/query?f=favorite:yes", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["canon_photo"] in ids
    assert e["sony_photo"] not in ids


@pytest.mark.slow
def test_query_star_range(query_env):
    """Star range filter narrows results."""
    e = query_env
    r = e["client"].get("/v1/query?f=stars:4%2B", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["canon_photo"] in ids  # 5 stars
    assert e["sony_photo"] not in ids  # 3 stars


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_pagination(query_env):
    """Keyset pagination returns different pages."""
    e = query_env
    # Page 1: limit 2
    r1 = e["client"].get("/v1/query?limit=2&sort=rel_path&dir=asc", headers=_headers(e["api_key"]))
    assert r1.status_code == 200
    data1 = r1.json()
    assert len(data1["items"]) == 2
    assert data1["next_cursor"] is not None

    # Page 2
    r2 = e["client"].get(
        f"/v1/query?limit=2&sort=rel_path&dir=asc&after={data1['next_cursor']}",
        headers=_headers(e["api_key"]),
    )
    assert r2.status_code == 200
    data2 = r2.json()
    assert len(data2["items"]) >= 1

    # No overlap
    ids1 = {i["asset_id"] for i in data1["items"]}
    ids2 = {i["asset_id"] for i in data2["items"]}
    assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# Text search with mocked Quickwit
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_text_search_with_structured_filter(query_env):
    """Text search + structured filter: candidate set intersected with SQL filters.

    Mock Quickwit to return canon_photo and sony_photo as text search hits.
    Then apply camera_make:Canon — only canon_photo should survive.
    """
    e = query_env

    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = [
        {"asset_id": e["canon_photo"], "score": 0.95},
        {"asset_id": e["sony_photo"], "score": 0.80},
    ]
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get(
            "/v1/query?f=query:landscape&f=camera_make:Canon",
            headers=_headers(e["api_key"]),
        )
    assert r.status_code == 200
    data = r.json()
    ids = [i["asset_id"] for i in data["items"]]
    assert e["canon_photo"] in ids
    assert e["sony_photo"] not in ids  # excluded by camera_make filter


@pytest.mark.slow
def test_query_text_search_context_annotation(query_env):
    """Text search results include search_context with score and hit_type."""
    e = query_env

    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = [
        {"asset_id": e["canon_photo"], "score": 0.9},
    ]
    mock_qw.search_tenant_scenes.return_value = []
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get("/v1/query?f=query:test", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    item = next(i for i in items if i["asset_id"] == e["canon_photo"])
    assert item["search_context"] is not None
    assert item["search_context"]["score"] == 0.9
    assert item["search_context"]["hit_type"] == "asset"


@pytest.mark.slow
def test_query_text_search_scene_context_preferred(query_env):
    """When same asset matches via asset and scene index, scene context wins."""
    e = query_env

    mock_qw = MagicMock()
    mock_qw.enabled = True
    mock_qw.search_tenant.return_value = [
        {"asset_id": e["sony_video"], "score": 0.5},
    ]
    mock_qw.search_tenant_scenes.return_value = [
        {
            "asset_id": e["sony_video"],
            "score": 0.7,
            "description": "A beautiful sunset scene",
            "start_ms": 5000,
            "end_ms": 10000,
        },
    ]
    mock_qw.search_tenant_transcripts.return_value = []

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get("/v1/query?f=query:sunset", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    items = r.json()["items"]
    item = next(i for i in items if i["asset_id"] == e["sony_video"])
    ctx = item["search_context"]
    assert ctx["hit_type"] == "scene"
    assert ctx["snippet"] == "A beautiful sunset scene"
    assert ctx["start_ms"] == 5000


# ---------------------------------------------------------------------------
# Empty results
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_no_matches(query_env):
    """Filter that matches nothing returns empty list."""
    e = query_env
    r = e["client"].get("/v1/query?f=camera_make:Nikon", headers=_headers(e["api_key"]))
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# Postgres fallback text search (Quickwit disabled)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_text_search_postgres_fallback(query_env):
    """Text search with Quickwit disabled falls back to Postgres ILIKE.

    This exercises the real database path — no mocks. Verifies that the
    query term is not wrapped in Quickwit syntax (parentheses) when passed
    to the postgres ILIKE search.
    """
    e = query_env

    # Mock Quickwit as disabled — forces postgres fallback
    mock_qw = MagicMock()
    mock_qw.enabled = False

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        r = e["client"].get(
            "/v1/query?f=query:woman",
            headers=_headers(e["api_key"]),
        )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["described_photo"] in ids, (
        f"Postgres fallback search for 'woman' should find the described photo. "
        f"Got {len(ids)} results: {ids}"
    )


@pytest.mark.slow
def test_query_text_search_postgres_fallback_with_structured_filter(query_env):
    """Postgres fallback text search + structured filter AND together."""
    e = query_env

    mock_qw = MagicMock()
    mock_qw.enabled = False

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        # Search for "woman" + camera_make:Canon → should find described_photo (Canon)
        r = e["client"].get(
            "/v1/query?f=query:woman&f=camera_make:Canon",
            headers=_headers(e["api_key"]),
        )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert e["described_photo"] in ids

    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=mock_qw):
        # Search for "woman" + camera_make:Sony → should find nothing
        r2 = e["client"].get(
            "/v1/query?f=query:woman&f=camera_make:Sony",
            headers=_headers(e["api_key"]),
        )
    assert r2.status_code == 200
    ids2 = [i["asset_id"] for i in r2.json()["items"]]
    assert e["described_photo"] not in ids2


# ---------------------------------------------------------------------------
# Capabilities endpoint
# ---------------------------------------------------------------------------

@pytest.mark.fast
def test_capabilities_endpoint(monkeypatch):
    """GET /v1/filters/capabilities returns all registered filters."""
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    with TestClient(app) as client:
        r = client.get("/v1/filters/capabilities")
    assert r.status_code == 200
    data = r.json()
    prefixes = {f["prefix"] for f in data["filters"]}
    assert "query" in prefixes
    assert "camera_make" in prefixes
    assert "iso" in prefixes
    assert "favorite" in prefixes
