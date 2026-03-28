"""API tests for video chunk API: init, claim, complete, fail, scenes, update scene vision."""

import os
import hashlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def video_api_client() -> tuple[TestClient, str, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create library, upsert video asset,
    init chunks via POST /v1/video/{asset_id}/chunks.
    Yields (client, api_key, library_id, asset_id, tenant_url).
    """
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
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "VideoAPITenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
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

            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "VideoAPILib", "root_path": "/videos"},
                    headers=auth,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                client.post(
                    "/v1/assets/upsert",
                    json={
                        "library_id": library_id,
                        "rel_path": "clip.mp4",
                        "file_size": 5000000,
                        "file_mtime": "2025-01-01T12:00:00Z",
                        "media_type": "video",
                    },
                    headers=auth,
                )
                r_asset = client.get(
                    "/v1/assets/by-path",
                    params={"library_id": library_id, "rel_path": "clip.mp4"},
                    headers=auth,
                )
                assert r_asset.status_code == 200
                asset_id = r_asset.json()["asset_id"]

                r_init = client.post(
                    f"/v1/video/{asset_id}/chunks",
                    json={"duration_sec": 100.0},
                    headers=auth,
                )
                assert r_init.status_code == 200
                assert r_init.json()["chunk_count"] >= 1

                yield client, api_key, library_id, asset_id, tenant_url

        _engines.clear()


@pytest.mark.slow
def test_init_chunks(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """POST /v1/video/{asset_id}/chunks with {duration_sec} returns {chunk_count, already_initialized} with 200."""
    client, api_key, library_id, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Init again is idempotent - already_initialized will be True
    r = client.post(
        f"/v1/video/{asset_id}/chunks",
        json={"duration_sec": 100.0},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert "chunk_count" in body
    assert "already_initialized" in body
    assert body["chunk_count"] >= 1


@pytest.mark.slow
def test_claim_next_chunk(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """After init, GET /v1/video/{asset_id}/chunks/next returns 200 with chunk or 204 if none pending."""
    client, api_key, _, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    assert r.status_code in (200, 204)
    if r.status_code == 200:
        body = r.json()
        assert "chunk_id" in body
        assert "worker_id" in body
        assert "chunk_index" in body
        assert "start_ts" in body
        assert "end_ts" in body


@pytest.mark.slow
def test_complete_chunk(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """Claim chunk, POST /v1/video/chunks/{chunk_id}/complete with scene data; assert 200."""
    client, api_key, _, asset_id, tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_claim = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    if r_claim.status_code == 204:
        pytest.skip("No pending chunks (all claimed/completed by other tests)")
    assert r_claim.status_code == 200
    chunk = r_claim.json()
    chunk_id = chunk["chunk_id"]
    worker_id = chunk["worker_id"]

    rep_frame_bytes = b"rep-frame-test:" + chunk_id.encode("utf-8")
    rep_frame_sha256 = hashlib.sha256(rep_frame_bytes).hexdigest()

    r = client.post(
        f"/v1/video/chunks/{chunk_id}/complete",
        json={
            "worker_id": worker_id,
            "scenes": [
                {
                    "scene_index": 0,
                    "start_ms": 0,
                    "end_ms": 5000,
                    "rep_frame_ms": 2500,
                    "description": "A scene",
                    "tags": ["test"],
                    "rep_frame_sha256": rep_frame_sha256,
                }
            ],
            "next_anchor_phash": "abc123",
            "next_scene_start_ms": 5000,
        },
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_id"] == chunk_id
    assert body["scenes_saved"] == 1

    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rep_frame_sha256 FROM video_scenes "
                    "WHERE asset_id = :asset_id AND rep_frame_sha256 = :sha"
                ),
                {"asset_id": asset_id, "sha": rep_frame_sha256},
            ).fetchone()
    finally:
        engine.dispose()

    assert row is not None
    assert row[0] == rep_frame_sha256


@pytest.mark.slow
def test_fail_chunk(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """Claim chunk, POST /v1/video/chunks/{chunk_id}/fail with error; assert 200."""
    client, api_key, _, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_claim = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    if r_claim.status_code == 204:
        pytest.skip("No pending chunks")
    assert r_claim.status_code == 200
    chunk = r_claim.json()
    chunk_id = chunk["chunk_id"]
    worker_id = chunk["worker_id"]

    r = client.post(
        f"/v1/video/chunks/{chunk_id}/fail",
        json={"worker_id": worker_id, "error_message": "Test failure"},
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chunk_id"] == chunk_id
    assert body["status"] == "failed"


@pytest.mark.slow
def test_get_scenes_empty(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """GET /v1/video/{asset_id}/scenes on asset with no completed scenes returns empty list."""
    client, api_key, library_id, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a second video asset with no chunks completed
    client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "empty.mp4",
            "file_size": 1000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
        },
        headers=auth,
    )
    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": "empty.mp4"},
        headers=auth,
    )
    assert r_asset.status_code == 200
    empty_asset_id = r_asset.json()["asset_id"]

    r = client.get(f"/v1/video/{empty_asset_id}/scenes", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["scenes"] == []


@pytest.mark.slow
def test_get_scenes_after_completion(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """Complete a chunk with scene payloads, then GET /v1/video/{asset_id}/scenes returns those scenes."""
    client, api_key, library_id, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Use a fresh asset to avoid interference
    client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "scenes.mp4",
            "file_size": 2000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
        },
        headers=auth,
    )
    r_asset = client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": "scenes.mp4"},
        headers=auth,
    )
    assert r_asset.status_code == 200
    aid = r_asset.json()["asset_id"]

    client.post(
        f"/v1/video/{aid}/chunks",
        json={"duration_sec": 60.0},
        headers=auth,
    )
    r_claim = client.get(f"/v1/video/{aid}/chunks/next", headers=auth)
    assert r_claim.status_code == 200
    chunk = r_claim.json()
    client.post(
        f"/v1/video/chunks/{chunk['chunk_id']}/complete",
        json={
            "worker_id": chunk["worker_id"],
            "scenes": [
                {
                    "scene_index": 0,
                    "start_ms": 0,
                    "end_ms": 10000,
                    "rep_frame_ms": 5000,
                    "description": "First scene",
                    "tags": ["outdoor"],
                }
            ],
            "next_anchor_phash": None,
            "next_scene_start_ms": None,
        },
        headers=auth,
    )

    r = client.get(f"/v1/video/{aid}/scenes", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert len(body["scenes"]) >= 1
    scene = body["scenes"][0]
    assert scene["description"] == "First scene"
    assert "scene_id" in scene


@pytest.mark.slow
def test_update_scene_vision(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """PATCH /v1/video/scenes/{scene_id} with {model_id, model_version, description, tags} returns updated scene."""
    client, api_key, library_id, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Ensure we have a scene
    r_claim = client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    if r_claim.status_code == 204:
        # Use a fresh asset and complete a chunk
        client.post(
            "/v1/assets/upsert",
            json={
                "library_id": library_id,
                "rel_path": "vision.mp4",
                "file_size": 3000,
                "file_mtime": "2025-01-01T12:00:00Z",
                "media_type": "video",
            },
            headers=auth,
        )
        r_asset = client.get(
            "/v1/assets/by-path",
            params={"library_id": library_id, "rel_path": "vision.mp4"},
            headers=auth,
        )
        aid = r_asset.json()["asset_id"]
        client.post(f"/v1/video/{aid}/chunks", json={"duration_sec": 60.0}, headers=auth)
        r_claim = client.get(f"/v1/video/{aid}/chunks/next", headers=auth)
        if r_claim.status_code == 204:
            pytest.skip("No chunks to claim")
        chunk = r_claim.json()
        client.post(
            f"/v1/video/chunks/{chunk['chunk_id']}/complete",
            json={
                "worker_id": chunk["worker_id"],
                "scenes": [
                    {
                        "scene_index": 0,
                        "start_ms": 0,
                        "end_ms": 5000,
                        "rep_frame_ms": 2500,
                        "description": "Initial",
                        "tags": [],
                    }
                ],
                "next_anchor_phash": None,
                "next_scene_start_ms": None,
            },
            headers=auth,
        )
        r_scenes = client.get(f"/v1/video/{aid}/scenes", headers=auth)
        asset_id = aid
    else:
        chunk = r_claim.json()
        client.post(
            f"/v1/video/chunks/{chunk['chunk_id']}/complete",
            json={
                "worker_id": chunk["worker_id"],
                "scenes": [
                    {
                        "scene_index": 0,
                        "start_ms": 0,
                        "end_ms": 5000,
                        "rep_frame_ms": 2500,
                        "description": "Initial",
                        "tags": [],
                    }
                ],
                "next_anchor_phash": None,
                "next_scene_start_ms": None,
            },
            headers=auth,
        )
        r_scenes = client.get(f"/v1/video/{asset_id}/scenes", headers=auth)

    assert r_scenes.status_code == 200
    scenes = r_scenes.json()["scenes"]
    assert len(scenes) >= 1
    scene_id = scenes[0]["scene_id"]

    r = client.patch(
        f"/v1/video/scenes/{scene_id}",
        json={
            "model_id": "test-vision-model",
            "model_version": "1",
            "description": "AI-generated description of the scene",
            "tags": ["indoor", "people"],
        },
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scene_id"] == scene_id
    assert body["status"] == "updated"


@pytest.mark.slow
def test_video_api_requires_auth(video_api_client: tuple[TestClient, str, str, str, str]) -> None:
    """Missing Authorization header on video endpoint returns 401."""
    client, _, _, asset_id, _tenant_url = video_api_client

    r = client.get(f"/v1/video/{asset_id}/scenes")
    assert r.status_code == 401
