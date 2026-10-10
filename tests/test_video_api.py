"""API tests for video chunk API: init, claim, complete, fail, scenes, update scene vision."""

import os
import hashlib
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
from tests.machine_lineage import made


@pytest.fixture(scope="module")
def video_api_client() -> tuple[TestClient, str, str, str, str]:
    """
    Two testcontainers Postgres; provision tenant DB; create library, upsert video asset,
    init chunks via POST /v1/video/{asset_id}/chunks.
    Yields (client, api_key, library_id, asset_id, tenant_url).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_postgres:
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

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "VideoAPITenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
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
    """After init, POST /v1/video/{asset_id}/chunks/next returns 200 with chunk or 204 if none pending."""
    client, api_key, library_id, _, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _video(client, auth, library_id, "claim.mp4", 60.0)

    r = client.post(f"/v1/video/{asset_id}/chunks/next", headers=auth)
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
    client, api_key, library_id, _, tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _video(client, auth, library_id, "complete.mp4", 60.0)

    r_claim = client.post(f"/v1/video/{asset_id}/chunks/next", headers=auth)
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
            "lineage": made("scenes"),
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
    client, api_key, library_id, _, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _video(client, auth, library_id, "fail.mp4", 60.0)

    r_claim = client.post(f"/v1/video/{asset_id}/chunks/next", headers=auth)
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
    r_claim = client.post(f"/v1/video/{aid}/chunks/next", headers=auth)
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
            "lineage": made("scenes"),
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
    r_claim = client.post(f"/v1/video/{asset_id}/chunks/next", headers=auth)
    if r_claim.status_code in (204, 409):
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
        r_claim = client.post(f"/v1/video/{aid}/chunks/next", headers=auth)
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
                "lineage": made("scenes"),
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
                "lineage": made("scenes"),
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
            "lineage": made("scene_vision"),
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


def _video(client: TestClient, auth: dict, library_id: str, rel_path: str, duration_sec: float) -> str:
    client.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": rel_path, "file_size": 3000,
                                          "file_mtime": "2025-01-01T12:00:00Z", "media_type": "video"},
                headers=auth)
    aid = client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path},
                     headers=auth).json()["asset_id"]
    assert client.post(f"/v1/video/{aid}/chunks", json={"duration_sec": duration_sec}, headers=auth).status_code == 200
    return aid


def _complete(client: TestClient, auth: dict, work: dict, starts: list[int]) -> None:
    r = client.post(f"/v1/video/chunks/{work['chunk_id']}/complete", headers=auth, json={
        "worker_id": work["worker_id"],
        # A client that numbered them itself sent 0, 1, ... again on resume: ignored.
        "scenes": [{"scene_index": 0, "start_ms": s, "end_ms": s + 1000, "rep_frame_ms": s} for s in starts],
        "next_anchor_phash": None, "next_scene_start_ms": None, "lineage": made("scenes")})
    assert r.status_code == 200, r.text


@pytest.mark.slow
def test_a_failed_chunk_stays_failed_until_the_next_run(video_api_client) -> None:
    """Review (Oct 9): every claim put failed chunks back and handed out the
    lowest, the one that just failed, so one bad chunk looped forever."""
    client, api_key, library_id, _asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = _video(client, auth, library_id, "loops.mp4", 60.0)  # two chunks

    first = client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json()
    assert first["chunk_index"] == 0
    r = client.post(f"/v1/video/chunks/{first['chunk_id']}/fail", headers=auth,
                    json={"worker_id": first["worker_id"], "error_message": "bad frames"})
    assert r.status_code == 200
    # Nothing more this run: chunks go in order.
    busy = client.post(f"/v1/video/{aid}/chunks/next", headers=auth)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "chunks_busy"

    # The next run (the scheduler's back-off decides when) starts it again.
    client.post(f"/v1/video/{aid}/chunks", json={"duration_sec": 60.0}, headers=auth)
    again = client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json()
    assert again["chunk_index"] == 0


@pytest.mark.slow
def test_a_chunk_under_way_holds_back_the_next(video_api_client) -> None:
    """Scenes are found, and numbered, in order: while one chunk is claimed
    (its lease not run out) the next isn't handed out."""
    client, api_key, library_id, _asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = _video(client, auth, library_id, "inorder.mp4", 60.0)
    first = client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json()
    assert client.post(f"/v1/video/{aid}/chunks/next", headers=auth).status_code == 409
    _complete(client, auth, first, [0])
    assert client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json()["chunk_index"] == 1


@pytest.mark.slow
def test_a_completed_chunk_cant_be_failed(video_api_client) -> None:
    """A /complete whose answer was lost, then /fail: the chunk stays
    completed (its scenes aren't found and saved twice)."""
    client, api_key, library_id, _asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = _video(client, auth, library_id, "lostanswer.mp4", 30.0)
    work = client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json()
    _complete(client, auth, work, [0])
    r = client.post(f"/v1/video/chunks/{work['chunk_id']}/fail", headers=auth,
                    json={"worker_id": work["worker_id"], "error_message": "timed out"})
    assert r.status_code == 409
    client.post(f"/v1/video/{aid}/chunks", json={"duration_sec": 30.0}, headers=auth)  # a new run
    assert client.post(f"/v1/video/{aid}/chunks/next", headers=auth).status_code == 204


@pytest.mark.slow
def test_claiming_a_chunk_is_a_post(video_api_client) -> None:
    client, api_key, _library_id, asset_id, _tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    assert client.get(f"/v1/video/{asset_id}/chunks/next", headers=auth).status_code == 405


@pytest.mark.slow
def test_a_resumed_clip_goes_on_numbering_its_scenes(video_api_client) -> None:
    """Review (Oct 9): the client numbered scenes from 0 in each run, so a
    clip resumed after a failure had two scene 0s."""
    client, api_key, library_id, _asset_id, tenant_url = video_api_client
    auth = {"Authorization": f"Bearer {api_key}"}
    aid = _video(client, auth, library_id, "resumed.mp4", 60.0)

    _complete(client, auth, client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json(), [0, 10000])
    # The run stops here; the next one claims the rest.
    _complete(client, auth, client.post(f"/v1/video/{aid}/chunks/next", headers=auth).json(), [30000, 40000])

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT scene_index, start_ms FROM video_scenes WHERE asset_id = :a"
                                 " ORDER BY start_ms"), {"a": aid}).all()
        assert [tuple(r) for r in rows] == [(0, 0), (1, 10000), (2, 30000), (3, 40000)]
        with pytest.raises(Exception, match="uq_video_scenes_asset_scene_index"):
            conn.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms,"
                              " rep_frame_ms, created_at) VALUES ('scn_dup', :a, 1, 0, 1, 0, now())"), {"a": aid})
    engine.dispose()
