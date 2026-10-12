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
import io
import json
import os
import secrets
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from src.server.storage.local import LocalStorage
from tests.conftest import PG_IMAGE, _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import made_json


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

    with PostgresContainer(PG_IMAGE) as control_pg:
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

        with patch("src.server.accounts.provision_tenant_database"):
            with TestClient(app) as bootstrap_client:
                r = bootstrap_client.post(
                    "/v1/admin/tenants",
                    json={"name": "ArtifactTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
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

            # Patch get_storage in every router that streams artifact files
            # so the test storage_root is used end-to-end. Each router imports
            # get_storage at module load, so they need separate patch targets.
            with patch("src.server.api.routers.artifacts.get_storage", return_value=storage), \
                 patch("src.server.api.routers.assets.get_storage", return_value=storage):
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

                    raw_client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": "photo.jpg",
                            "file_size": 1000,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image",
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
    """A real JPEG of random pixels, `size` px square: image artifacts are
    re-encoded when stored, and no two of these are alike."""
    buf = io.BytesIO()
    Image.frombytes("RGB", (size, size), secrets.token_bytes(size * size * 3)).save(buf, "JPEG")
    return buf.getvalue()


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
        data={"width": "2048", "height": "1365", "lineage": made_json("proxy")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "key" in body
    assert body["sha256"] == _sha256(storage.abs_path(body["key"]).read_bytes()), "the stored file's"
    assert body["key"].endswith(".webp")


@pytest.mark.slow
def test_upload_proxy_writes_file_to_storage(artifact_env) -> None:
    auth, _, _, asset_id, storage, _, _ = artifact_env
    content = _make_jpeg(512)

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    assert r.status_code == 200
    key = r.json()["key"]
    assert storage.abs_path(key).exists()
    stored = Image.open(storage.abs_path(key))
    assert (stored.format, stored.size) == ("WEBP", (512, 512)), "re-encoded as WebP, the same picture"


@pytest.mark.slow
def test_upload_proxy_persists_sha256_in_db(artifact_env) -> None:
    auth, _, _, asset_id, storage, tenant_url, _ = artifact_env
    content = _make_jpeg()

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    expected = _sha256(storage.abs_path(r.json()["key"]).read_bytes())

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
        data={"lineage": made_json("proxy")},
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
    auth, _, _, asset_id, storage, _, _ = artifact_env
    content = _make_jpeg(64)

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},  # the proxy producer makes thumbnails
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == _sha256(storage.abs_path(body["key"]).read_bytes()), "the stored file's"


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
        data={"lineage": made_json("proxy")},
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
    auth, _, _, asset_id, storage, tenant_url, _ = artifact_env
    content = _make_jpeg(64)

    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},  # the proxy producer makes thumbnails
    )
    expected = _sha256(storage.abs_path(r.json()["key"]).read_bytes())

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
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": "clip.mp4",
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
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
        data={"lineage": made_json("video_preview")},
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
    rel_path = f"scene_rep_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
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
    stored = storage.abs_path(body["key"]).read_bytes()
    assert body["sha256"] == _sha256(stored), "the stored file's"
    assert Image.open(io.BytesIO(stored)).format == "JPEG"


@pytest.mark.slow
def test_upload_scene_rep_does_not_overwrite_video_preview_key(artifact_env) -> None:
    """Regression: a scene_rep upload must not touch assets.video_preview_key.

    A scene_rep is a per-scene JPG keyframe stored under /scenes/; its on-disk
    path is derived from (asset_id, rep_frame_ms) and is not recorded in any
    asset-level column. The video_preview_key column is reserved for the MP4
    hover-preview clip uploaded via artifact_type='video_preview' and is what
    the /v1/assets/{id}/preview streaming endpoint serves.

    Bug: a fall-through `else` branch in the artifact upload dispatch caused
    every scene_rep upload to call set_video_preview() with the scene_rep's
    JPG key, clobbering the real MP4 preview path. The /preview endpoint then
    streamed the JPG bytes with Content-Type: video/mp4 and the browser <video>
    element couldn't decode the result.
    """
    auth, _, library_id, _, _, tenant_url, _ = artifact_env

    rel_path = f"scene_rep_no_clobber_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    video_asset_id = r_vid.json()["asset_id"]

    # Step 1: upload an MP4 video preview, capturing the resulting key.
    mp4_content = b"FTYP" + secrets.token_bytes(512)
    r_prev = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
        data={"lineage": made_json("video_preview")},
    )
    assert r_prev.status_code == 200, r_prev.text
    preview_key = r_prev.json()["key"]
    assert "/previews/" in preview_key
    assert preview_key.endswith(".mp4")

    # Step 2: upload a scene_rep JPG to the same asset.
    r_scene = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        files={"file": ("scene.jpg", _make_jpeg(96), "image/jpeg")},
        data={"rep_frame_ms": "12345"},
    )
    assert r_scene.status_code == 200, r_scene.text
    scene_key = r_scene.json()["key"]
    assert "/scenes/" in scene_key
    assert scene_key.endswith(".jpg")

    # Step 3: video_preview_key must still point at the MP4 preview, not the JPG.
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT video_preview_key FROM assets WHERE asset_id = :id"),
            {"id": video_asset_id},
        ).fetchone()
    engine.dispose()
    assert row[0] == preview_key, (
        f"scene_rep upload clobbered video_preview_key: expected {preview_key!r}, got {row[0]!r}"
    )


@pytest.mark.slow
def test_upload_scene_rep_requires_rep_frame_ms(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    rel_path = f"scene_rep_missing_ms_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
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

    r1 = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content_v1, "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    r2 = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content_v2, "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    assert r2.status_code == 200
    stored_v2 = _sha256(storage.abs_path(r2.json()["key"]).read_bytes())
    assert r2.json()["sha256"] == stored_v2 != r1.json()["sha256"]

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT proxy_sha256 FROM assets WHERE asset_id = :id"),
            {"id": asset_id},
        ).fetchone()
    engine.dispose()
    assert row[0] == stored_v2


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
        data={"lineage": made_json("proxy")},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_upload_missing_auth_returns_401(artifact_env) -> None:
    _, raw_client, _, asset_id, _, _, _ = artifact_env

    r = raw_client.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", b"data", "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    assert r.status_code == 401


@pytest.mark.slow
def test_upload_soft_deleted_asset_returns_404(artifact_env) -> None:
    """Uploading to a trashed asset returns 404."""
    auth, _, library_id, _, _, tenant_url, _ = artifact_env

    # Create a fresh asset.
    rel_path = f"to_trash_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
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
        data={"lineage": made_json("proxy")},
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

    up = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )

    r = auth.get(f"/v1/assets/{asset_id}/artifacts/proxy")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert _sha256(r.content) == up.json()["sha256"], "what was stored"


@pytest.mark.slow
def test_download_thumbnail_returns_bytes(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env
    content = _make_jpeg(64)

    up = auth.post(
        f"/v1/assets/{asset_id}/artifacts/thumbnail",
        files={"file": ("thumb.jpg", content, "image/jpeg")},
        data={"lineage": made_json("proxy")},  # the proxy producer makes thumbnails
    )

    r = auth.get(f"/v1/assets/{asset_id}/artifacts/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert _sha256(r.content) == up.json()["sha256"], "what was stored"


@pytest.mark.slow
def test_download_video_preview_returns_bytes(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    rel_path = f"dl_clip_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
        },
    )
    r_vid = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    video_asset_id = r_vid.json()["asset_id"]

    mp4_content = b"FTYP" + secrets.token_bytes(512)
    auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
        data={"lineage": made_json("video_preview")},
    )

    r = auth.get(f"/v1/assets/{video_asset_id}/artifacts/video_preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == mp4_content


@pytest.mark.slow
def test_download_scene_rep_returns_bytes(artifact_env) -> None:
    auth, _, library_id, _, _, _, _ = artifact_env

    rel_path = f"dl_scene_rep_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
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
    assert _sha256(r.content) == r_up.json()["sha256"], "what was stored, as it was stored"


@pytest.mark.slow
def test_download_proxy_not_ready_returns_404(artifact_env) -> None:
    """Asset with no proxy uploaded returns 404 with artifact_not_ready code."""
    auth, _, library_id, _, _, _, _ = artifact_env

    rel_path = f"fresh_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
        },
    )
    r_a = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    fresh_id = r_a.json()["asset_id"]

    r = auth.get(f"/v1/assets/{fresh_id}/artifacts/proxy")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_not_ready"


@pytest.mark.slow
def test_download_proxy_file_missing_returns_404(artifact_env) -> None:
    """Key set in DB but file deleted from disk → 404 with artifact_missing code."""
    auth, _, library_id, _, storage, _, _ = artifact_env

    # Use a fresh asset so deleting the file doesn't affect other tests.
    rel_path = f"missing_{secrets.token_hex(4)}.jpg"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 100,
            "file_mtime": "2025-01-01T00:00:00Z",
            "media_type": "image",
        },
    )
    r_a = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    isolated_id = r_a.json()["asset_id"]

    r_up = auth.post(
        f"/v1/assets/{isolated_id}/artifacts/proxy",
        files={"file": ("proxy.jpg", _make_jpeg(), "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    key = r_up.json()["key"]
    storage.abs_path(key).unlink()  # remove file, leave key in DB

    r = auth.get(f"/v1/assets/{isolated_id}/artifacts/proxy")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_missing"


@pytest.mark.slow
def test_a_missing_file_is_reported_and_upkeep_repairs_it(artifact_env) -> None:
    """The GET changes nothing (the key stays); it reports the clip, and
    upkeep clears the key of a file still gone so the producer makes it again."""
    auth, _, library_id, _, storage, tenant_url, _ = artifact_env
    rel_path = f"repair_{secrets.token_hex(4)}.jpg"
    auth.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": rel_path, "file_size": 100,
                                         "file_mtime": "2025-01-01T00:00:00Z", "media_type": "image"})
    asset_id = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path}).json()["asset_id"]
    key = auth.post(f"/v1/assets/{asset_id}/artifacts/proxy",
                    files={"file": ("proxy.jpg", _make_jpeg(), "image/jpeg")},
                    data={"lineage": made_json("proxy")}).json()["key"]
    storage.abs_path(key).unlink()

    for _ in range(2):  # reported once, however often it's asked for
        assert auth.get(f"/v1/assets/{asset_id}/artifacts/proxy").status_code == 404
    assert auth.get(f"/v1/assets/{asset_id}").json()["proxy_key"] == key  # the GET wrote nothing to the clip
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            reported = json.loads(conn.execute(text(
                "SELECT value FROM system_metadata WHERE key = 'artifacts.missing'")).scalar())
    finally:
        engine.dispose()
    assert reported.count(f"{asset_id}:proxy") == 1

    with patch("src.server.missing_artifacts.get_storage", return_value=storage):
        r = auth.post("/v1/upkeep")
    assert r.status_code == 200, r.text
    assert r.json()["missing_files"]["cleared"] >= 1
    assert auth.get(f"/v1/assets/{asset_id}").json()["proxy_key"] is None


def _clip_with_preview_and_thumbnail(auth, library_id: str) -> tuple[str, str, str]:
    """A photo with its preview (1024 px) and its thumbnail: its id and their keys."""
    rel_path = f"thumb_{secrets.token_hex(4)}.jpg"
    auth.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": rel_path, "file_size": 100,
                                         "file_mtime": "2025-01-01T00:00:00Z", "media_type": "image"})
    asset_id = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path}).json()["asset_id"]
    proxy = auth.post(f"/v1/assets/{asset_id}/artifacts/proxy", files={"file": ("p.jpg", _make_jpeg(1024), "image/jpeg")},
                      data={"lineage": made_json("proxy")}).json()["key"]
    thumb = auth.post(f"/v1/assets/{asset_id}/artifacts/thumbnail",
                      files={"file": ("t.jpg", _make_jpeg(512), "image/jpeg")},
                      data={"lineage": made_json("proxy")}).json()["key"]  # the proxy producer makes thumbnails
    return asset_id, proxy, thumb


def _upkeep(auth, storage) -> dict:
    with patch("src.server.missing_artifacts.get_storage", return_value=storage):
        r = auth.post("/v1/upkeep")
    assert r.status_code == 200, r.text
    return r.json()["missing_files"]


@pytest.mark.slow
def test_a_missing_thumbnail_is_made_again_from_the_preview_which_stays(artifact_env) -> None:
    """The server made it from the preview at ingest, so it makes it again:
    the preview isn't taken out of service, and no scan is needed."""
    auth, _, library_id, _, storage, tenant_url, _ = artifact_env
    asset_id, proxy, thumb = _clip_with_preview_and_thumbnail(auth, library_id)
    preview = storage.abs_path(proxy).read_bytes()
    storage.abs_path(thumb).unlink()
    assert auth.get(f"/v1/assets/{asset_id}/artifacts/thumbnail").status_code == 404  # reported

    assert _upkeep(auth, storage)["remade"] >= 1
    clip = auth.get(f"/v1/assets/{asset_id}").json()
    assert (clip["proxy_key"], clip["thumbnail_key"]) == (proxy, thumb)
    assert storage.abs_path(proxy).read_bytes() == preview
    r = auth.get(f"/v1/assets/{asset_id}/artifacts/thumbnail")
    assert r.status_code == 200, r.text
    made = Image.open(io.BytesIO(r.content))
    assert made.format == "WEBP" and max(made.size) == 512
    engine = create_engine(tenant_url)
    try:
        with engine.connect() as conn:
            recorded = conn.execute(text("SELECT thumbnail_sha256 FROM assets WHERE asset_id = :a"),
                                    {"a": asset_id}).scalar()
    finally:
        engine.dispose()
    assert recorded == hashlib.sha256(storage.abs_path(thumb).read_bytes()).hexdigest()


@pytest.mark.slow
def test_a_missing_thumbnail_without_its_preview_leaves_both_for_a_scan(artifact_env) -> None:
    auth, _, library_id, _, storage, *_ = artifact_env
    asset_id, proxy, thumb = _clip_with_preview_and_thumbnail(auth, library_id)
    storage.abs_path(proxy).unlink()
    storage.abs_path(thumb).unlink()
    assert auth.get(f"/v1/assets/{asset_id}/artifacts/thumbnail").status_code == 404
    assert _upkeep(auth, storage)["cleared"] >= 1
    clip = auth.get(f"/v1/assets/{asset_id}").json()
    assert (clip["proxy_key"], clip["thumbnail_key"]) == (None, None)


@pytest.mark.fast
@pytest.mark.parametrize("layout", ["no_data_dir", "empty_data_dir", "no_account_dir", "empty_account_dir"])
def test_upkeep_repairs_nothing_while_the_data_folder_is_gone(tmp_path, layout) -> None:
    """An unmounted DATA_DIR (or account folder) looks like every file gone:
    no key is cleared and the reports stay for the next upkeep."""
    from unittest.mock import MagicMock

    from src.server import missing_artifacts

    data = tmp_path / "data"
    if layout != "no_data_dir":
        data.mkdir()
    if layout in ("no_account_dir", "empty_account_dir"):
        (data / "ten_other").mkdir()
    if layout == "empty_account_dir":
        (data / "ten_1").mkdir()
    session = MagicMock()
    session.execute.return_value.scalar.side_effect = [json.dumps(["ast_1:proxy"]), "ten_1/lib_1/proxies/00/ast_1.webp"]
    with patch("src.server.missing_artifacts.get_storage", return_value=LocalStorage(str(data))):
        assert missing_artifacts.repair(session) == {"checked": 0, "cleared": 0, "remade": 0}
    assert not any("UPDATE" in str(c.args[0]) for c in session.execute.call_args_list)
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


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


# ---------------------------------------------------------------------------
# /v1/assets/{asset_id}/artifacts/video_preview — what the web UI fetches for
# the hover preview, and the Mac and iPhone lightbox. It is the only preview
# route: /proxy, /thumbnail and /preview are gone.
# ---------------------------------------------------------------------------


def _create_video_asset(auth, library_id: str, *, prefix: str) -> str:
    """Helper: upsert a video asset and return its id."""
    rel_path = f"{prefix}_{secrets.token_hex(4)}.mp4"
    auth.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 5000,
            "file_mtime": "2025-01-01T12:00:00Z",
            "media_type": "video",
        },
    )
    r = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


@pytest.mark.slow
def test_preview_endpoint_streams_uploaded_mp4(artifact_env) -> None:
    """The video_preview artifact returns the bytes uploaded as video_preview."""
    auth, _, library_id, _, _, _, _ = artifact_env
    video_asset_id = _create_video_asset(auth, library_id, prefix="preview_stream")

    mp4_content = b"FTYP" + secrets.token_bytes(2048)
    r_up = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
        data={"lineage": made_json("video_preview")},
    )
    assert r_up.status_code == 200, r_up.text

    r = auth.get(f"/v1/assets/{video_asset_id}/artifacts/video_preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == mp4_content


@pytest.mark.slow
def test_preview_endpoint_serves_mp4_after_scene_rep_upload(artifact_env) -> None:
    """Regression: /preview must keep serving the MP4 even after scene_rep uploads.

    This is the end-to-end smoke test for the bug that motivated this PR.
    Before the fix, uploading a scene_rep would clobber video_preview_key with
    the JPG path, and /preview would stream the JPG bytes labelled as video/mp4
    — exactly what the browser <video> element couldn't decode.
    """
    auth, _, library_id, _, _, _, _ = artifact_env
    video_asset_id = _create_video_asset(auth, library_id, prefix="preview_after_scene")

    mp4_content = b"FTYP" + secrets.token_bytes(2048)
    r_up = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/video_preview",
        files={"file": ("preview.mp4", mp4_content, "video/mp4")},
        data={"lineage": made_json("video_preview")},
    )
    assert r_up.status_code == 200, r_up.text

    # Interleave a scene_rep upload — the bug clobbered video_preview_key here.
    jpeg_content = _make_jpeg(256)
    r_scene = auth.post(
        f"/v1/assets/{video_asset_id}/artifacts/scene_rep",
        files={"file": ("scene.jpg", jpeg_content, "image/jpeg")},
        data={"rep_frame_ms": "5000"},
    )
    assert r_scene.status_code == 200, r_scene.text

    r = auth.get(f"/v1/assets/{video_asset_id}/artifacts/video_preview")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == mp4_content, (
        "preview endpoint returned non-MP4 bytes after a scene_rep upload — "
        "video_preview_key was clobbered"
    )
    # And just to be explicit: it must NOT be the JPG bytes.
    assert r.content != jpeg_content


@pytest.mark.slow
def test_preview_endpoint_returns_404_when_no_preview(artifact_env) -> None:
    """Video asset with no video_preview yet → 404 (so the UI can show a placeholder
    rather than render <video> with bogus bytes)."""
    auth, _, library_id, _, _, _, _ = artifact_env
    video_asset_id = _create_video_asset(auth, library_id, prefix="preview_missing")

    r = auth.get(f"/v1/assets/{video_asset_id}/artifacts/video_preview")
    assert r.status_code == 404


@pytest.mark.slow
def test_preview_endpoint_rejects_image_assets(artifact_env) -> None:
    """An image has no video preview."""
    auth, _, _, image_asset_id, _, _, _ = artifact_env
    r = auth.get(f"/v1/assets/{image_asset_id}/artifacts/video_preview")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_not_ready"


@pytest.mark.slow
@pytest.mark.parametrize("route", ["proxy", "thumbnail", "preview"])
def test_the_duplicate_picture_routes_are_gone(artifact_env, route) -> None:
    """One route per artifact: /artifacts/{type}."""
    auth, _, _, asset_id, _, _, _ = artifact_env
    assert auth.get(f"/v1/assets/{asset_id}/{route}").status_code == 404


@pytest.mark.slow
@pytest.mark.parametrize("artifact_type", ["proxy", "thumbnail"])
def test_an_uploaded_image_is_stored_without_the_originals_metadata(artifact_env, artifact_type) -> None:
    from tests.test_preview_metadata import METADATA, photo, upright_with_profile

    auth, _, _, asset_id, storage, _, _ = artifact_env
    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/{artifact_type}",
        files={"file": ("p.webp", photo("WEBP"), "image/webp")},
        data={"lineage": made_json("proxy")},
    )
    assert r.status_code == 200, r.text
    stored = storage.abs_path(r.json()["key"]).read_bytes()
    assert [m for m in METADATA if m in stored] == []
    assert upright_with_profile(stored)


@pytest.mark.slow
def test_an_uploaded_scene_frame_is_stored_without_the_originals_metadata(artifact_env) -> None:
    from tests.test_preview_metadata import METADATA, photo

    auth, _, library_id, _, storage, _, _ = artifact_env
    rel_path = f"frame_check_{secrets.token_hex(4)}.mp4"
    auth.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": rel_path, "file_size": 2048,
                                         "file_mtime": "2025-01-01T00:00:00Z", "media_type": "video"})
    video = auth.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path}).json()
    r = auth.post(
        f"/v1/assets/{video['asset_id']}/artifacts/scene_rep",
        files={"file": ("frame.jpg", photo("JPEG"), "image/jpeg")},
        data={"rep_frame_ms": "1000"},
    )
    assert r.status_code == 200, r.text
    assert [m for m in METADATA if m in storage.abs_path(r.json()["key"]).read_bytes()] == []


@pytest.mark.slow
def test_an_upload_that_isnt_a_jpeg_png_or_webp_is_refused(artifact_env) -> None:
    auth, _, _, asset_id, _, _, _ = artifact_env
    r = auth.post(
        f"/v1/assets/{asset_id}/artifacts/proxy",
        files={"file": ("p.jpg", b"\xff\xd8\xff" + secrets.token_bytes(64), "image/jpeg")},
        data={"lineage": made_json("proxy")},
    )
    assert r.status_code == 400 and "JPEG, PNG or WebP" in r.text
