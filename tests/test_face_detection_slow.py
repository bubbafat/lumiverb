"""Slow integration tests for face detection API endpoints."""

from __future__ import annotations

import os
import secrets
from typing import Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from sqlalchemy import create_engine, text
from src.core.database import _engines, get_control_session
from src.repository.control_plane import TenantDbRoutingRepository
from src.repository.tenant import AssetRepository, LibraryRepository
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def face_client() -> Tuple[_AuthClient, str, str]:
    """Set up two Postgres containers, tenant, library. Yields (auth_client, library_id, tenant_url)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "FaceDetectionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                auth_client = _AuthClient(client, api_key)
                lib_name = "FaceLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/faces"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _create_asset(auth_client: _AuthClient, library_id: str, name: str) -> str:
    """Create a test asset and return its asset_id."""
    rel_path = f"photos/{name}.jpg"
    r = auth_client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 1000,
            "file_mtime": "2024-01-01T00:00:00Z",
            "media_type": "image",
        },
    )
    assert r.status_code == 200, (r.status_code, r.text)
    r2 = auth_client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
    )
    assert r2.status_code == 200, (r2.status_code, r2.text)
    return r2.json()["asset_id"]


@pytest.mark.slow
def test_submit_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/assets/{id}/faces creates face records and sets face_count."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_submit")

    r = auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "detection_model": "insightface",
            "detection_model_version": "buffalo_l",
            "faces": [
                {
                    "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
                    "detection_confidence": 0.97,
                    "embedding": [0.1] * 512,
                },
                {
                    "bounding_box": {"x": 0.5, "y": 0.3, "w": 0.1, "h": 0.15},
                    "detection_confidence": 0.85,
                    "embedding": [0.2] * 512,
                },
            ],
        },
    )
    assert r.status_code == 201, (r.status_code, r.text)
    data = r.json()
    assert data["face_count"] == 2
    assert len(data["face_ids"]) == 2


@pytest.mark.slow
def test_list_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/{id}/faces returns submitted faces."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_list")

    # Submit faces
    auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "faces": [
                {
                    "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3},
                    "detection_confidence": 0.95,
                    "embedding": [0.5] * 512,
                },
            ],
        },
    )

    # List faces
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert len(data["faces"]) == 1
    face = data["faces"][0]
    assert face["bounding_box"]["x"] == pytest.approx(0.1)
    assert face["detection_confidence"] == pytest.approx(0.95)
    assert face["person"] is None


@pytest.mark.slow
def test_submit_faces_idempotent(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Resubmitting faces replaces the old ones."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_idempotent")

    # First submission: 2 faces
    auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "faces": [
                {"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9},
                {"bounding_box": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}, "detection_confidence": 0.8},
            ],
        },
    )

    # Second submission: 1 face (replaces)
    r = auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "faces": [
                {"bounding_box": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2}, "detection_confidence": 0.99},
            ],
        },
    )
    assert r.status_code == 201
    assert r.json()["face_count"] == 1

    # Verify only 1 face exists
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert len(r.json()["faces"]) == 1


@pytest.mark.slow
def test_submit_no_faces_sets_zero(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Submitting empty faces array sets face_count=0 (processed, no faces)."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_zero")

    r = auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={"faces": []},
    )
    assert r.status_code == 201
    assert r.json()["face_count"] == 0


@pytest.mark.slow
def test_submit_faces_not_found(face_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/assets/{id}/faces returns 404 for unknown asset."""
    auth_client, _, _ = face_client
    r = auth_client.post(
        "/v1/assets/nonexistent_asset_id/faces",
        json={"faces": []},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_list_faces_not_found(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/{id}/faces returns 404 for unknown asset."""
    auth_client, _, _ = face_client
    r = auth_client.get("/v1/assets/nonexistent_asset_id/faces")
    assert r.status_code == 404


@pytest.mark.slow
def test_repair_summary_missing_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """repair-summary includes missing_faces count."""
    auth_client, library_id, _ = face_client

    # Create an asset without face detection
    asset_id = _create_asset(auth_client, library_id, "face_repair_summary")

    r = auth_client.get(f"/v1/assets/repair-summary?library_id={library_id}")
    assert r.status_code == 200
    data = r.json()
    assert "missing_faces" in data
    # At least this asset should be missing faces
    assert data["missing_faces"] >= 1


@pytest.mark.slow
def test_page_missing_faces_filter(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/page?missing_faces=true filters correctly."""
    auth_client, library_id, _ = face_client

    # Create asset and submit faces for it
    asset_with = _create_asset(auth_client, library_id, "face_page_with")
    auth_client.post(
        f"/v1/assets/{asset_with}/faces",
        json={"faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}]},
    )

    # Create asset without faces
    asset_without = _create_asset(auth_client, library_id, "face_page_without")

    # Page with missing_faces=true should include the unprocessed asset
    r = auth_client.get(f"/v1/assets/page?library_id={library_id}&missing_faces=true")
    assert r.status_code == 200
    items = r.json()["items"]
    ids = [i["asset_id"] for i in items]
    assert asset_without in ids
    assert asset_with not in ids


@pytest.mark.slow
def test_submit_faces_without_embedding(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Faces can be submitted without embedding (embedding=null)."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_no_embed")

    r = auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "faces": [
                {
                    "bounding_box": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
                    "detection_confidence": 0.88,
                },
            ],
        },
    )
    assert r.status_code == 201
    assert r.json()["face_count"] == 1

    # Verify it's retrievable
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert len(r.json()["faces"]) == 1


@pytest.mark.slow
def test_has_faces_filter_on_page(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/page?has_faces=true filters to assets with detected faces."""
    auth_client, library_id, _ = face_client

    # Asset with faces
    asset_with = _create_asset(auth_client, library_id, "has_faces_with")
    auth_client.post(
        f"/v1/assets/{asset_with}/faces",
        json={"faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}]},
    )

    # Asset with no faces (processed, face_count=0)
    asset_zero = _create_asset(auth_client, library_id, "has_faces_zero")
    auth_client.post(f"/v1/assets/{asset_zero}/faces", json={"faces": []})

    # has_faces=true -> only asset_with
    r = auth_client.get(f"/v1/assets/page?library_id={library_id}&has_faces=true")
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert asset_with in ids
    assert asset_zero not in ids

    # has_faces=false -> includes asset_zero and unprocessed
    r = auth_client.get(f"/v1/assets/page?library_id={library_id}&has_faces=false")
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert asset_zero in ids
    assert asset_with not in ids


@pytest.mark.slow
def test_has_faces_filter_on_browse(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/browse?has_faces=true filters via SQL in UnifiedBrowseRepository."""
    auth_client, library_id, _ = face_client

    # Asset with faces
    asset_with = _create_asset(auth_client, library_id, "browse_faces_with")
    auth_client.post(
        f"/v1/assets/{asset_with}/faces",
        json={"faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}]},
    )

    # Asset without faces (processed)
    asset_zero = _create_asset(auth_client, library_id, "browse_faces_zero")
    auth_client.post(f"/v1/assets/{asset_zero}/faces", json={"faces": []})

    # has_faces=true -> only asset_with
    r = auth_client.get("/v1/browse?has_faces=true")
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert asset_with in ids
    assert asset_zero not in ids


@pytest.mark.slow
def test_has_faces_filter_on_search(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/search?has_faces=true post-filters search results correctly."""
    auth_client, library_id, tenant_url = face_client

    import json as _json

    # Create asset with faces + metadata (so Postgres search can find it)
    asset_with = _create_asset(auth_client, library_id, "search_faces_sunset")
    auth_client.post(
        f"/v1/assets/{asset_with}/faces",
        json={"faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}]},
    )
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, generated_at, data) "
            "VALUES (:mid, :aid, 'test', '1', NOW(), :data)"
        ), {"mid": "meta_sf_" + asset_with[:8], "aid": asset_with,
            "data": _json.dumps({"description": "sunset over mountains", "tags": ["sunset"]})})
    engine.dispose()

    # Create asset without faces but also searchable
    asset_no = _create_asset(auth_client, library_id, "search_nofaces_sunset")
    auth_client.post(f"/v1/assets/{asset_no}/faces", json={"faces": []})
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, generated_at, data) "
            "VALUES (:mid, :aid, 'test', '1', NOW(), :data)"
        ), {"mid": "meta_snf_" + asset_no[:8], "aid": asset_no,
            "data": _json.dumps({"description": "sunset at the beach", "tags": ["sunset"]})})
    engine.dispose()

    # Search with has_faces=true (Postgres fallback)
    os.environ["QUICKWIT_ENABLED"] = "false"
    os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
    get_settings.cache_clear()

    r = auth_client.get("/v1/search", params={"q": "sunset", "has_faces": "true"})
    assert r.status_code == 200
    data = r.json()
    hit_ids = [h["asset_id"] for h in data["hits"]]
    assert asset_with in hit_ids
    assert asset_no not in hit_ids


@pytest.mark.slow
def test_facets_has_face_count(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/facets returns has_face_count field."""
    auth_client, library_id, _ = face_client

    # Create an asset with faces
    asset_id = _create_asset(auth_client, library_id, "facets_face")
    auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={"faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}]},
    )

    r = auth_client.get(f"/v1/assets/facets?library_id={library_id}")
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert "has_face_count" in data
    assert data["has_face_count"] >= 1


@pytest.mark.slow
def test_list_faces_response_shape(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Regression: GET /v1/assets/{id}/faces response shape matches what the lightbox expects."""
    auth_client, library_id, _ = face_client
    asset_id = _create_asset(auth_client, library_id, "face_shape")

    auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "faces": [
                {
                    "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.25},
                    "detection_confidence": 0.92,
                    "embedding": [0.3] * 512,
                },
            ],
        },
    )

    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert r.status_code == 200
    data = r.json()
    assert "faces" in data
    face = data["faces"][0]
    # All fields the lightbox overlay depends on
    assert "face_id" in face
    assert "bounding_box" in face
    bbox = face["bounding_box"]
    for key in ("x", "y", "w", "h"):
        assert key in bbox, f"bounding_box missing '{key}'"
        assert isinstance(bbox[key], (int, float))
    assert "detection_confidence" in face
    assert "person" in face  # null until clustering ships
