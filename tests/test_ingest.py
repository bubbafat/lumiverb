"""Tests for the atomic ingest endpoint POST /v1/assets/{asset_id}/ingest.

All tests are slow (testcontainers Postgres). Reuses the same two-container
setup as test_artifacts.py.
"""

from __future__ import annotations

import hashlib
import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from src.storage.local import LocalStorage
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _make_test_image(width: int = 300, height: int = 200, fmt: str = "JPEG") -> bytes:
    """Generate a small test image as bytes."""
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingest_env(tmp_path_factory):
    """Two Postgres containers, one tenant, one library, one asset, temp storage."""
    storage_root = tmp_path_factory.mktemp("ingest_storage")
    storage = LocalStorage(str(storage_root))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
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
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        from unittest.mock import patch

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap_client:
                r = bootstrap_client.post(
                    "/v1/admin/tenants",
                    json={"name": "IngestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with (
                patch("src.api.routers.artifacts.get_storage", return_value=storage),
                patch("src.api.routers.ingest.get_storage", return_value=storage),
            ):
                with TestClient(app) as raw_client:
                    auth_headers = {"Authorization": f"Bearer {api_key}"}

                    r_lib = raw_client.post(
                        "/v1/libraries",
                        json={"name": "IngestLib", "root_path": "/media"},
                        headers=auth_headers,
                    )
                    assert r_lib.status_code == 200
                    library_id = r_lib.json()["library_id"]

                    r_scan = raw_client.post(
                        "/v1/scans",
                        json={"library_id": library_id, "status": "running"},
                        headers=auth_headers,
                    )
                    assert r_scan.status_code == 200
                    scan_id = r_scan.json()["scan_id"]

                    # Helper to create assets
                    def create_asset(rel_path: str) -> str:
                        raw_client.post(
                            "/v1/assets/upsert",
                            json={
                                "library_id": library_id,
                                "rel_path": rel_path,
                                "file_size": 1000,
                                "file_mtime": "2025-01-01T12:00:00Z",
                                "media_type": "image",
                                "scan_id": scan_id,
                            },
                            headers=auth_headers,
                        )
                        r = raw_client.get(
                            "/v1/assets/by-path",
                            params={"library_id": library_id, "rel_path": rel_path},
                            headers=auth_headers,
                        )
                        assert r.status_code == 200
                        return r.json()["asset_id"]

                    asset_id = create_asset("photo.jpg")
                    yield raw_client, auth_headers, library_id, asset_id, storage, create_asset

    _engines.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ingest_proxy_only(ingest_env) -> None:
    """Ingest with just a proxy — should normalize to WebP and generate thumbnail."""
    client, auth, library_id, asset_id, storage, _ = ingest_env

    proxy_bytes = _make_test_image(400, 300)
    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("proxy.jpg", io.BytesIO(proxy_bytes), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["asset_id"] == asset_id
    assert data["status"] == "proxy_ready"
    assert data["proxy_key"].endswith(".webp")  # key naming convention
    assert data["thumbnail_key"]
    assert data["proxy_sha256"]
    assert data["thumbnail_sha256"]

    # Verify files on disk are WebP
    proxy_path = storage.abs_path(data["proxy_key"])
    assert proxy_path.exists()
    proxy_img = Image.open(proxy_path)
    assert proxy_img.format == "WEBP"

    thumb_path = storage.abs_path(data["thumbnail_key"])
    assert thumb_path.exists()
    thumb_img = Image.open(thumb_path)
    assert thumb_img.format == "WEBP"
    assert max(thumb_img.size) <= 512


@pytest.mark.slow
def test_ingest_large_proxy_is_resized(ingest_env) -> None:
    """A proxy larger than 2048px is resized down."""
    client, auth, library_id, asset_id, storage, _ = ingest_env

    proxy_bytes = _make_test_image(4000, 3000)
    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("big.jpg", io.BytesIO(proxy_bytes), "image/jpeg")},
    )
    assert r.status_code == 200
    data = r.json()

    proxy_path = storage.abs_path(data["proxy_key"])
    proxy_img = Image.open(proxy_path)
    assert max(proxy_img.size) <= 2048


@pytest.mark.slow
def test_ingest_small_proxy_not_upscaled(ingest_env) -> None:
    """A proxy smaller than 2048px is not upscaled."""
    client, auth, library_id, _, storage, create_asset = ingest_env
    asset_id = create_asset("small.jpg")

    proxy_bytes = _make_test_image(800, 600)
    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("small.jpg", io.BytesIO(proxy_bytes), "image/jpeg")},
    )
    assert r.status_code == 200
    data = r.json()

    proxy_path = storage.abs_path(data["proxy_key"])
    proxy_img = Image.open(proxy_path)
    # Should be close to original (Pillow may adjust slightly for WebP)
    assert max(proxy_img.size) <= 800


@pytest.mark.slow
def test_ingest_with_exif(ingest_env) -> None:
    """Ingest with proxy + EXIF metadata."""
    client, auth, library_id, _, storage, create_asset = ingest_env
    asset_id = create_asset("exif_test.jpg")

    exif_data = {
        "sha256": "abc123",
        "camera_make": "Sony",
        "camera_model": "A7R IV",
        "taken_at": "2025-06-15T14:30:00Z",
        "gps_lat": 47.6062,
        "gps_lon": -122.3321,
    }

    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={"exif": json.dumps(exif_data)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "proxy_ready"

    # Verify EXIF was stored by reading the asset detail
    r_detail = client.get(f"/v1/assets/{asset_id}", headers=auth)
    assert r_detail.status_code == 200
    detail = r_detail.json()
    assert detail["camera_make"] == "Sony"
    assert detail["camera_model"] == "A7R IV"


@pytest.mark.slow
def test_ingest_with_vision(ingest_env) -> None:
    """Ingest with proxy + vision — status should be 'described'."""
    client, auth, library_id, _, storage, create_asset = ingest_env
    asset_id = create_asset("vision_test.jpg")

    vision_data = {
        "model_id": "gpt-4o",
        "model_version": "1",
        "description": "A sunset over the ocean",
        "tags": ["sunset", "ocean", "landscape"],
    }

    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={"vision": json.dumps(vision_data)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "described"


@pytest.mark.slow
def test_ingest_with_all_fields(ingest_env) -> None:
    """Ingest with proxy + EXIF + vision + embeddings — full atomic create."""
    client, auth, library_id, _, storage, create_asset = ingest_env
    asset_id = create_asset("full_test.jpg")

    exif_data = {"camera_make": "Canon", "camera_model": "R5"}
    vision_data = {
        "model_id": "gpt-4o",
        "model_version": "1",
        "description": "Mountain landscape",
        "tags": ["mountain", "nature"],
    }
    embeddings_data = [
        {"model_id": "openclip", "model_version": "1", "vector": [0.1] * 512},
    ]

    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={
            "width": "4000",
            "height": "3000",
            "exif": json.dumps(exif_data),
            "vision": json.dumps(vision_data),
            "embeddings": json.dumps(embeddings_data),
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "described"
    assert data["width"] == 4000
    assert data["height"] == 3000


@pytest.mark.slow
def test_ingest_missing_asset_returns_404(ingest_env) -> None:
    """Ingest for a nonexistent asset returns 404."""
    client, auth, *_ = ingest_env

    r = client.post(
        "/v1/assets/ast_nonexistent/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_ingest_empty_proxy_returns_400(ingest_env) -> None:
    """Empty proxy file should return 400."""
    client, auth, _, asset_id, *_ = ingest_env

    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("empty.jpg", io.BytesIO(b""), "image/jpeg")},
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_ingest_invalid_exif_json_returns_400(ingest_env) -> None:
    """Invalid EXIF JSON should return 400."""
    client, auth, _, asset_id, *_ = ingest_env

    r = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={"exif": "not valid json{{{"},
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_ingest_is_idempotent(ingest_env) -> None:
    """Calling ingest twice on the same asset should overwrite cleanly."""
    client, auth, library_id, _, storage, create_asset = ingest_env
    asset_id = create_asset("idempotent_test.jpg")

    proxy1 = _make_test_image(400, 300)
    r1 = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("v1.jpg", io.BytesIO(proxy1), "image/jpeg")},
    )
    assert r1.status_code == 200

    proxy2 = _make_test_image(500, 400)
    r2 = client.post(
        f"/v1/assets/{asset_id}/ingest",
        headers=auth,
        files={"proxy": ("v2.jpg", io.BytesIO(proxy2), "image/jpeg")},
    )
    assert r2.status_code == 200

    # Second ingest should have different sha256 (different image)
    assert r1.json()["proxy_sha256"] != r2.json()["proxy_sha256"]


# ---------------------------------------------------------------------------
# POST /v1/ingest — create-on-ingest tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_on_ingest_creates_asset(ingest_env) -> None:
    """POST /v1/ingest creates the asset record and ingests atomically."""
    client, auth, library_id, _, storage, _ = ingest_env

    r = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={
            "library_id": library_id,
            "rel_path": "new_photo.jpg",
            "file_size": "5000",
            "media_type": "image",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created"] is True
    assert data["status"] == "proxy_ready"
    assert data["asset_id"].startswith("ast_")

    # Verify the asset exists on the server
    r_detail = client.get(f"/v1/assets/{data['asset_id']}", headers=auth)
    assert r_detail.status_code == 200
    assert r_detail.json()["rel_path"] == "new_photo.jpg"


@pytest.mark.slow
def test_create_on_ingest_with_vision(ingest_env) -> None:
    """Create-on-ingest with vision data sets status to described."""
    client, auth, library_id, _, storage, _ = ingest_env

    vision_data = {
        "model_id": "gpt-4o",
        "model_version": "1",
        "description": "A beautiful photo",
        "tags": ["beautiful"],
    }

    r = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={
            "library_id": library_id,
            "rel_path": "new_vision.jpg",
            "file_size": "5000",
            "vision": json.dumps(vision_data),
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "described"
    assert r.json()["created"] is True


@pytest.mark.slow
def test_create_on_ingest_idempotent(ingest_env) -> None:
    """Calling create-on-ingest twice with the same rel_path updates instead of duplicating."""
    client, auth, library_id, _, storage, _ = ingest_env

    rel_path = "idempotent_create.jpg"

    r1 = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("v1.jpg", io.BytesIO(_make_test_image(400, 300)), "image/jpeg")},
        data={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "5000",
        },
    )
    assert r1.status_code == 200
    assert r1.json()["created"] is True

    r2 = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("v2.jpg", io.BytesIO(_make_test_image(500, 400)), "image/jpeg")},
        data={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "6000",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r1.json()["asset_id"] == r2.json()["asset_id"]


@pytest.mark.slow
def test_create_on_ingest_invalid_library(ingest_env) -> None:
    """Create-on-ingest with nonexistent library returns 404."""
    client, auth, *_ = ingest_env

    r = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={
            "library_id": "lib_nonexistent",
            "rel_path": "photo.jpg",
            "file_size": "5000",
        },
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_create_on_ingest_blocked_by_library_exclude_filter(ingest_env) -> None:
    """POST /v1/ingest returns 422 when rel_path matches a library exclude filter."""
    client, auth, library_id, _, storage, _ = ingest_env

    # Add an exclude filter to the library
    r_filter = client.post(
        f"/v1/libraries/{library_id}/filters",
        json={"type": "exclude", "pattern": "**/blocked/**"},
        headers=auth,
    )
    assert r_filter.status_code == 201
    filter_id = r_filter.json()["filter_id"]

    # Attempt ingest with a path matching the exclude filter
    r = client.post(
        "/v1/ingest",
        headers=auth,
        files={"proxy": ("photo.jpg", io.BytesIO(_make_test_image()), "image/jpeg")},
        data={
            "library_id": library_id,
            "rel_path": "some/blocked/photo.jpg",
            "file_size": "5000",
        },
    )
    assert r.status_code == 422

    # Clean up: remove the filter so other tests are unaffected
    client.delete(
        f"/v1/libraries/{library_id}/filters/{filter_id}",
        headers=auth,
    )


# ---------------------------------------------------------------------------
# _normalize_proxy unit tests (WebP fast path)
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_normalize_proxy_webp_fast_path() -> None:
    """_normalize_proxy returns input bytes unchanged for valid WebP within size limits."""
    from src.api.routers.ingest import _normalize_proxy

    webp_bytes = _make_test_image(800, 600, fmt="WEBP")
    result_bytes, w, h = _normalize_proxy(webp_bytes)
    assert result_bytes is webp_bytes  # exact same object — no re-encoding
    assert w == 800
    assert h == 600


@pytest.mark.fast
def test_normalize_proxy_jpeg_reencoded_to_webp() -> None:
    """_normalize_proxy re-encodes JPEG input to WebP."""
    from src.api.routers.ingest import _normalize_proxy

    jpeg_bytes = _make_test_image(400, 300, fmt="JPEG")
    result_bytes, w, h = _normalize_proxy(jpeg_bytes)
    assert result_bytes is not jpeg_bytes  # different object
    # Verify the output is WebP
    img = Image.open(io.BytesIO(result_bytes))
    assert img.format == "WEBP"
    assert w == 400
    assert h == 300
