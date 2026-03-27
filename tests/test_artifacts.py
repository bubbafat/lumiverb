"""Tests for artifact upload (Phase 2) and download (Phase 3) endpoints.

  POST /v1/assets/{asset_id}/artifacts/{artifact_type}
  GET  /v1/assets/{asset_id}/artifacts/{artifact_type}

All tests are slow (testcontainers Postgres).  A single module-scoped fixture
spins up the two Postgres containers and provisions a tenant + library + image
asset that most tests share.  Tests that need a fresh or isolated asset create
those resources themselves using the live client.
"""

from __future__ import annotations

import hashlib
import os
import secrets

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from src.storage.local import LocalStorage
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def artifact_env(tmp_path_factory):
    """
    Two Postgres containers (control + tenant), one tenant, one library, one image
    asset, and a temp LocalStorage.

    Yields:
        (client: _AuthClient, raw_client: TestClient, library_id: str,
         asset_id: str, storage: LocalStorage, tenant_url: str, api_key: str)
    """
    storage_root = tmp_path_factory.mktemp("artifact_storage")
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
                    json={"name": "ArtifactTenant", "plan": "free"},
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

            with patch("src.api.routers.artifacts.get_storage", return_value=storage):
                with TestClient(app) as raw_client:
                    auth = _AuthClient(raw_client, api_key)
                    auth_headers = {"Authorization": f"Bearer {api_key}"}

                    r_lib = raw_client.post(
                        "/v1/libraries",
                        json={"name": "ArtifactLib", "root_path": "/media"},
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

                    raw_client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": "photo.jpg",
                            "file_size": 1000,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image",
                            "scan_id": scan_id,
                        },
                        headers=auth_headers,
                    )
                    r_asset = raw_client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": "photo.jpg"},
                        headers=auth_headers,
                    )
                    assert r_asset.status_code == 200
                    asset_id = r_asset.json()["asset_id"]

                    yield auth, raw_client, library_id, asset_id, storage, tenant_url, api_key

        _engines.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg(size: int = 256) -> bytes:
    return b"\xff\xd8\xff" + secrets.token_bytes(size)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Slow tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_upload_proxy_returns_key_and_sha256(artifact_env) -> None:
    auth, _, _, asset_id, storage, _, _ = artifact_env
    content = _make_jpeg()

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
        data={"width": "2048", "height": "1365"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "key" in body
    assert body["sha256"] == _sha256(content)
    assert body["key"].endswith(".webp")


@pytest.mark.slow
def test_upload_proxy_writes_file_to_storage(artifact_env) -> None:
    auth, _, _, asset_id, storage, _, _ = artifact_env
    content = _make_jpeg(512)

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
    )
    assert r.status_code == 200
    key = r.json()["key"]
    assert storage.abs_path(key).exists()
    assert storage.abs_path(key).read_bytes() == content


@pytest.mark.slow
def test_upload_proxy_persists_sha256_in_db(artifact_env) -> None:
    auth, _, _, asset_id, _, tenant_url, _ = artifact_env
    content = _make_jpeg()
    expected = _sha256(content)

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
    )

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT proxy_sha256, proxy_key FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()

    assert row is not None
    assert row[0] == expected
    assert row[1] is not None


@pytest.mark.slow
def test_upload_proxy_does_not_advance_status(artifact_env) -> None:
    """Uploading a proxy via the artifact endpoint must not set status = proxy_ready."""
    auth, _, _, asset_id, _, tenant_url, _ = artifact_env
    content = _make_jpeg()

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
    )

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()

    assert row is not None
    assert row[0] != "proxy_ready", "upload endpoint must not advance asset status"


@pytest.mark.slow
def test_upload_thumbnail_returns_key_and_sha256(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env
    content = _make_jpeg(64)

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == _sha256(content)


@pytest.mark.slow
def test_upload_thumbnail_does_not_overwrite_proxy_key(artifact_env) -> None:
    """Uploading a thumbnail must not touch proxy_key or proxy_sha256."""
    auth, _, _, asset_id, _, tenant_url, _ = artifact_env

    # Capture proxy state before thumbnail upload.
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT proxy_key, proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", _make_jpeg(64), "image/jpeg")},
    )

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT proxy_key, proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()

    assert before[0] == after[0], "proxy_key must not change after thumbnail upload"
    assert before[1] == after[1], "proxy_sha256 must not change after thumbnail upload"


@pytest.mark.slow
def test_upload_thumbnail_persists_sha256_in_db(artifact_env) -> None:
    auth, _, _, asset_id, _, tenant_url, _ = artifact_env
    content = _make_jpeg(64)
    expected = _sha256(content)

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
    )

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT thumbnail_sha256, thumbnail_key FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()

    assert row[0] == expected
    assert row[1] is not None


@pytest.mark.slow
def test_upload_video_preview_writes_file_no_sha256_col(artifact_env) -> None:
    """video_preview upload stores the file and returns a key but has no sha256 column."""
    auth, _, library_id, _, storage, tenant_url, _ = artifact_env
    from unittest.mock import patch

    # Need a video asset.
    raw_client_ref = None
    api_key_ref = None

    # Re-use the raw_client from artifact_env by calling the fixture differently.
    # Instead, use the auth client directly to create a video asset.
    r_scan = auth.post(
        "/v1/scans",
        json={"library_id": library_id, "status": "running"},
    )
    scan_id = r_scan.json()["scan_id"]
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "clip.mp4",
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
    )
    r_vid = auth.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": "clip.mp4"},
    )
    assert r_vid.status_code == 200, r_vid.text
    video_asset_id = r_vid.json()["asset_id"]

    mp4_content = b"FTYP" + secrets.token_bytes(512)
    r = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "key" in body
    assert body["sha256"] == _sha256(mp4_content)
    assert storage.abs_path(body["key"]).exists()

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT video_preview_key FROM assets WHERE asset_id = :id"),
            {"id": video_asset_id},
        ).fetchone()
    engine.dispose()
    assert row[0] == body["key"]


@pytest.mark.slow
def test_upload_scene_rep_writes_file_to_scenes_path(artifact_env) -> None:
    auth, _, library_id, _, storage, _, _ = artifact_env

    # Create a video asset for scene representative frame uploads.
    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"scene_rep_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    assert r_vid.status_code == 200, r_vid.text
    video_asset_id = r_vid.json()["asset_id"]

    content = _make_jpeg(96)
    r = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        files={"file": ("scene.jpg", content, "image/jpeg")},
        data={"rep_frame_ms": "12345"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "/scenes/" in body["key"]
    assert body["sha256"] == _sha256(content)
    assert storage.abs_path(body["key"]).exists()
    assert storage.abs_path(body["key"]).read_bytes() == content


@pytest.mark.slow
def test_upload_scene_rep_requires_rep_frame_ms(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"scene_rep_missing_ms_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    video_asset_id = r_vid.json()["asset_id"]

    r = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        files={"file": ("scene.jpg", _make_jpeg(64), "image/jpeg")},
    )
    assert r.status_code == 400
    assert "rep_frame_ms is required" in r.text


@pytest.mark.slow
def test_reupload_proxy_overwrites_sha256(artifact_env) -> None:
    """A second upload of the same artifact type replaces key, file, and sha256."""
    auth, _, _, asset_id, storage, tenant_url, _ = artifact_env

    content_v1 = _make_jpeg(100)
    content_v2 = _make_jpeg(200)  # different bytes → different hash

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content_v1, "image/jpeg")},
    )
    r2 = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content_v2, "image/jpeg")},
    )
    assert r2.status_code == 200
    assert r2.json()["sha256"] == _sha256(content_v2)

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()
    assert row[0] == _sha256(content_v2)


@pytest.mark.slow
def test_upload_invalid_artifact_type_returns_400(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/original",
        files={"file": ("file.jpg", b"data", "image/jpeg")},
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_upload_unknown_asset_returns_404(artifact_env) -> None:
    auth, _, _, _, _, _, _ = artifact_env

    r = auth.post(
        "/v1/assets/ast_nonexistent_000000000000/artifacts/proxy",
        files={"file": ("proxy.jpg", b"data", "image/jpeg")},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_upload_missing_auth_returns_401(artifact_env) -> None:
    _, raw_client, _, asset_id, _, _, _ = artifact_env

    r = raw_client.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", b"data", "image/jpeg")},
    )
    assert r.status_code == 401


@pytest.mark.slow
def test_upload_soft_deleted_asset_returns_404(artifact_env) -> None:
    """Uploading to a trashed asset returns 404."""
    auth, _, library_id, _, _, tenant_url, _ = artifact_env

    # Create a fresh asset.
    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"to_trash_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
            "scan_id": scan_id,
        },
    )
    r_a = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    trash_id = r_a.json()["asset_id"]

    # Soft-delete directly in the DB (avoids needing a delete method on _AuthClient).
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE assets SET deleted_at = now() WHERE asset_id = :id"),
            {"id": trash_id},
        )
        conn.commit()
    engine.dispose()

    r = auth.post(
        f"/v1/assets/{trash_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", _make_jpeg(), "image/jpeg")},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Phase 3: Download tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_download_proxy_returns_bytes(artifact_env) -> None:
    """Upload a proxy then download it — content and content-type must match."""
    auth, _, _, asset_id, _, _, _ = artifact_env
    content = _make_jpeg(300)

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
    )

    r = auth.get(f"/v1/assets/{asset_id}/artifacts/proxy")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.content == content


@pytest.mark.slow
def test_download_thumbnail_returns_bytes(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env
    content = _make_jpeg(64)

    auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
    )

    r = auth.get(f"/v1/assets/{asset_id}/artifacts/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.content == content


@pytest.mark.slow
def test_download_video_preview_returns_bytes(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"dl_clip_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    video_asset_id = r_vid.json()["asset_id"]

    mp4_content = b"FTYP" + secrets.token_bytes(512)
    auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
    )

    r = auth.get(f"/v1/assets/{video_asset_id}/artifacts/video_preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == mp4_content


@pytest.mark.slow
def test_download_scene_rep_returns_bytes(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"dl_scene_rep_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
            "scan_id": scan_id,
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    video_asset_id = r_vid.json()["asset_id"]

    content = _make_jpeg(128)
    rep_frame_ms = 7777
    r_up = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        files={"file": ("scene.jpg", content, "image/jpeg")},
        data={"rep_frame_ms": str(rep_frame_ms)},
    )
    assert r_up.status_code == 200, r_up.text

    r = auth.get(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        params={"rep_frame_ms": rep_frame_ms},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == content
    assert _sha256(r.content) == _sha256(content)


@pytest.mark.slow
def test_download_proxy_not_ready_returns_404(artifact_env) -> None:
    """Asset with no proxy uploaded returns 404 with artifact_not_ready code."""
    auth, _, library_id, _, _, _, _ = artifact_env

    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"fresh_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
            "scan_id": scan_id,
        },
    )
    r_a = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    fresh_id = r_a.json()["asset_id"]

    r = auth.get(f"/v1/assets/{fresh_id}/artifacts/proxy")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "artifact_not_ready"


@pytest.mark.slow
def test_download_proxy_file_missing_returns_404(artifact_env) -> None:
    """Key set in DB but file deleted from disk → 404 with artifact_missing code."""
    auth, _, library_id, _, storage, _, _ = artifact_env

    # Use a fresh asset so deleting the file doesn't affect other tests.
    r_scan = auth.post("/v1/scans", json={"library_id": library_id, "status": "running"})
    scan_id = r_scan.json()["scan_id"]
    rel_path = f"missing_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
            "scan_id": scan_id,
        },
    )
    r_a = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    isolated_id = r_a.json()["asset_id"]

    r_up = auth.post(
        f"/v1/assets/{isolated_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", _make_jpeg(), "image/jpeg")},
    )
    key = r_up.json()["key"]
    storage.abs_path(key).unlink()  # remove file, leave key in DB

    r = auth.get(f"/v1/assets/{isolated_id}/artifacts/proxy")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "artifact_missing"


@pytest.mark.slow
def test_download_invalid_type_returns_400(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env
    r = auth.get(f"/v1/assets/{asset_id}/artifacts/original")
    assert r.status_code == 400


@pytest.mark.slow
def test_download_unknown_asset_returns_404(artifact_env) -> None:
    auth, _, _, _, _, _, _ = artifact_env
    r = auth.get("/v1/assets/ast_nonexistent_000000000000/artifacts/proxy")
    assert r.status_code == 404


@pytest.mark.slow
def test_download_missing_auth_returns_401(artifact_env) -> None:
    _, raw_client, _, asset_id, _, _, _ = artifact_env
    r = raw_client.get(f"/v1/assets/{asset_id}/artifacts/proxy")
    assert r.status_code == 401
