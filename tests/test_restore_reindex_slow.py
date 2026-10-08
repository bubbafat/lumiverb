"""Restoring a trashed asset puts it back in search.

Trashing deletes an asset's documents from all three Quickwit indexes
(assets, scenes, transcript segments). Restore has to queue the asset and
its scenes for the next search sync and re-index its transcript segments,
which the sync sweep never does, or a restored clip stays missing from
text search for good.
"""

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
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

SRT = "1\n00:00:00,000 --> 00:00:02,000\nthe quick brown fox\n\n2\n00:00:02,000 --> 00:00:04,000\njumps over\n"


@pytest.fixture(scope="module")
def env():
    """Control + tenant DB. Yields (client, auth, library_id, tenant_engine)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-restore"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants", json={"name": "RestoreTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-restore"},
                )
                assert r.status_code == 200, r.text
                tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            tenant_engine = create_engine(tenant_url)
            with TestClient(app) as client:
                auth = {"Authorization": f"Bearer {api_key}"}
                library_id = client.post(
                    "/v1/libraries", json={"name": "RestoreLib", "root_path": "/footage"}, headers=auth,
                ).json()["library_id"]
                yield client, auth, library_id, tenant_engine
            tenant_engine.dispose()
    _engines.clear()


def _video(client, auth, library_id, rel_path) -> str:
    r = client.post(
        "/v1/assets/upsert",
        json={"library_id": library_id, "rel_path": rel_path, "file_size": 1000,
              "file_mtime": "2025-01-01T12:00:00Z", "media_type": "video"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    return client.get(
        "/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path}, headers=auth,
    ).json()["asset_id"]


@pytest.mark.slow
def test_restore_queues_the_asset_and_its_scenes_for_search_sync(env):
    client, auth, library_id, engine = env
    asset_id = _video(client, auth, library_id, "restore/synced.mov")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms, created_at, search_synced_at)"
            " VALUES ('scn_restore_1', :a, 0, 0, 1000, 500, now(), now())"
        ), {"a": asset_id})
        conn.execute(text("UPDATE assets SET search_synced_at = now() WHERE asset_id = :a"), {"a": asset_id})

    assert client.delete(f"/v1/assets/{asset_id}", headers=auth).status_code == 204
    assert client.post(f"/v1/assets/{asset_id}/restore", headers=auth).status_code == 204

    with engine.connect() as conn:
        asset_synced = conn.execute(
            text("SELECT search_synced_at FROM assets WHERE asset_id = :a"), {"a": asset_id}
        ).scalar()
        scene_synced = conn.execute(
            text("SELECT search_synced_at FROM video_scenes WHERE asset_id = :a"), {"a": asset_id}
        ).scalar()
    assert asset_synced is None
    assert scene_synced is None


@pytest.mark.slow
def test_restore_reindexes_transcript_segments(env):
    client, auth, library_id, _ = env
    asset_id = _video(client, auth, library_id, "restore/transcribed.mov")
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"srt": SRT, "language": "en"}, headers=auth)
    assert r.status_code == 200, r.text
    assert client.delete(f"/v1/assets/{asset_id}", headers=auth).status_code == 204

    qw = MagicMock()
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=qw):
        assert client.post(f"/v1/assets/{asset_id}/restore", headers=auth).status_code == 204

    assert qw.ingest_tenant_transcript_documents.call_count == 1
    docs = list(qw.ingest_tenant_transcript_documents.call_args.args[1])
    assert [d["text"] for d in docs] == ["the quick brown fox", "jumps over"]
    assert {d["asset_id"] for d in docs} == {asset_id}


def _ingest_video(client, auth, library_id, rel_path) -> str:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post(
        "/v1/ingest",
        data={"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": "video",
              "width": "64", "height": "64"},
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _synced(engine, asset_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT search_synced_at FROM assets WHERE asset_id = :a"), {"a": asset_id}
        ).scalar()


def _mark_synced_with_transcript(client, auth, engine, asset_id):
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"srt": SRT, "language": "en"}, headers=auth)
    assert r.status_code == 200, r.text
    with engine.begin() as conn:
        conn.execute(text("UPDATE assets SET search_synced_at = now() WHERE asset_id = :a"), {"a": asset_id})


@pytest.mark.slow
def test_a_file_that_reappears_comes_back_in_search(env):
    client, auth, library_id, engine = env
    asset_id = _ingest_video(client, auth, library_id, "reappear/clip.mov")
    _mark_synced_with_transcript(client, auth, engine, asset_id)
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"}, headers=auth)
    assert r.status_code == 200, r.text

    qw = MagicMock()
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=qw):
        _ingest_video(client, auth, library_id, "reappear/clip.mov")  # the scan finds it again

    assert _synced(engine, asset_id) is None
    assert qw.ingest_tenant_transcript_documents.call_count == 1


@pytest.mark.slow
@pytest.mark.parametrize("how", ["restore_with_clips", "restore_clips"])
def test_clips_restored_through_a_project_come_back_in_search(env, how):
    client, auth, library_id, engine = env
    asset_id = _ingest_video(client, auth, library_id, f"project-restore/{how}.mov")
    _mark_synced_with_transcript(client, auth, engine, asset_id)
    project_id = client.post("/v1/projects", json={"name": how, "asset_ids": [asset_id]}, headers=auth).json()["project_id"]
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user", "remove_from_projects": True}, headers=auth)
    assert r.status_code == 200, r.text

    qw = MagicMock()
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=qw):
        if how == "restore_with_clips":
            client.delete(f"/v1/projects/{project_id}", headers=auth)
            r = client.post(f"/v1/projects/{project_id}/restore", json={"with_clips": True}, headers=auth)
        else:
            r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=auth)
        assert r.status_code == 200, r.text

    assert _synced(engine, asset_id) is None
    assert qw.ingest_tenant_transcript_documents.call_count == 1
