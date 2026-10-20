"""A clip back in sight is found again at once.

Search reads only the clips in sight (it joins active_assets), so a clip
trashed or gone missing is never found, and nothing is taken out of search
for it: its words, its scenes' and its transcript's segments are kept with
it (ADR-019). Back in sight, from the trash, a project or its file found
again, it's found by all of them at once, with no sync to wait for.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made

SRT = "1\n00:00:00,000 --> 00:00:02,000\nthe quick brown fox\n\n2\n00:00:02,000 --> 00:00:04,000\njumps over\n"
SEGMENTS = ["the quick brown fox", "jumps over"]


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

        with patch("src.server.accounts.provision_tenant_database"), TestClient(app) as client:
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


def _video(engine, library_id, rel_path) -> str:
    with Session(engine) as session:
        return make_clip(session, library_id, rel_path, media_type="video")


def _found(env, words: str) -> dict[str, dict]:
    """What a search of the library finds: {asset_id: its search context}."""
    client, auth, library_id, _ = env
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", f"query:{words}")], headers=auth)
    assert r.status_code == 200, r.text
    return {i["asset_id"]: i["search_context"] for i in r.json()["items"]}


def _segments(engine, asset_id) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT text FROM transcript_segments WHERE asset_id = :a ORDER BY seq"),
                                 {"a": asset_id}).scalars())


def _transcribe(env, asset_id) -> None:
    client, auth, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"source": "manual", "srt": SRT, "language": "en"},
                    headers=auth)
    assert r.status_code == 200, r.text


def _found_by_its_transcript(env, asset_id) -> None:
    hit = _found(env, "fox").get(asset_id)
    assert hit is not None, "not found by its transcript"
    assert (hit["hit_type"], hit["start_ms"], hit["end_ms"], hit["snippet"]) == ("transcript", 0, 2000, SEGMENTS[0])


@pytest.mark.slow
def test_a_restored_clip_is_found_again_by_its_words_and_its_scenes(env):
    client, auth, library_id, engine = env
    asset_id = _video(engine, library_id, "restore/lighthouse.mov")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms, description,"
            " tags, created_at) VALUES ('scn_restore_1', :a, 0, 0, 1000, 500, 'a trawler unloading', '[]', now())"
        ), {"a": asset_id})
    r = client.post("/v1/upkeep/search-sync", headers=auth)  # its own words: written by the sweep
    assert r.status_code == 200, r.text
    assert _found(env, "lighthouse")[asset_id]["hit_type"] == "asset"
    assert _found(env, "trawler")[asset_id]["hit_type"] == "scene"

    assert client.delete(f"/v1/assets/{asset_id}", headers=auth).status_code == 204
    assert asset_id not in _found(env, "lighthouse") and asset_id not in _found(env, "trawler")

    assert client.post(f"/v1/assets/{asset_id}/restore", headers=auth).status_code == 204
    assert _found(env, "lighthouse")[asset_id]["hit_type"] == "asset"
    assert _found(env, "trawler")[asset_id]["hit_type"] == "scene"


@pytest.mark.slow
def test_a_restored_clip_is_found_again_by_its_transcript(env):
    client, auth, library_id, engine = env
    asset_id = _video(engine, library_id, "restore/transcribed.mov")
    _transcribe(env, asset_id)
    _found_by_its_transcript(env, asset_id)
    assert client.delete(f"/v1/assets/{asset_id}", headers=auth).status_code == 204
    assert asset_id not in _found(env, "fox")
    assert _segments(engine, asset_id) == SEGMENTS  # kept with it

    assert client.post(f"/v1/assets/{asset_id}/restore", headers=auth).status_code == 204
    _found_by_its_transcript(env, asset_id)
    assert _segments(engine, asset_id) == SEGMENTS


def _ingest_video(client, auth, library_id, rel_path) -> str:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post(
        "/v1/ingest",
        data={"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": "video",
              "width": "64", "height": "64", "lineage": ingest_made()},
        files={"lumiverb.preview": ("proxy.jpg", buf, "image/jpeg")},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


@pytest.mark.slow
def test_a_file_that_reappears_comes_back_in_search(env):
    client, auth, library_id, engine = env
    asset_id = _ingest_video(client, auth, library_id, "reappear/clip.mov")
    _transcribe(env, asset_id)
    _found_by_its_transcript(env, asset_id)
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "missing"}, headers=auth)
    assert r.status_code == 200, r.text
    assert asset_id not in _found(env, "fox")

    assert _ingest_video(client, auth, library_id, "reappear/clip.mov") == asset_id  # the scan finds it again
    _found_by_its_transcript(env, asset_id)
    assert _segments(engine, asset_id) == SEGMENTS


@pytest.mark.slow
@pytest.mark.parametrize("how", ["restore_with_clips", "restore_clips"])
def test_clips_restored_through_a_project_come_back_in_search(env, how):
    client, auth, library_id, engine = env
    asset_id = _ingest_video(client, auth, library_id, f"project-restore/{how}.mov")
    _transcribe(env, asset_id)
    project_id = client.post("/v1/projects", json={"name": how, "asset_ids": [asset_id]}, headers=auth).json()["project_id"]
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user", "remove_from_projects": True}, headers=auth)
    assert r.status_code == 200, r.text
    assert asset_id not in _found(env, "fox")

    if how == "restore_with_clips":
        client.delete(f"/v1/projects/{project_id}", headers=auth)
        r = client.post(f"/v1/projects/{project_id}/restore", json={"with_clips": True}, headers=auth)
    else:
        r = client.post(f"/v1/projects/{project_id}/restore-clips", headers=auth)
    assert r.status_code == 200, r.text

    _found_by_its_transcript(env, asset_id)
    assert _segments(engine, asset_id) == SEGMENTS
