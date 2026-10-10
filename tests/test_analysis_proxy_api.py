"""Analysis proxies on the server (ADR-016 phase 2).

A full-length, low-resolution copy of each video, with audio, kept on the
brain. Transcription, scene detection and scene vision read it instead of
the original, so enrichment continues while the storage holding the
originals sleeps. They are not edit proxies.

Uses testcontainers Postgres (control + tenant) and a temp LocalStorage.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from src.server.storage.local import LocalStorage
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made, made_json

_STORAGE_USERS = ("artifacts", "assets", "trash", "ingest", "video")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yield (client, headers, library_id, storage, tenant_id, tenant_url)."""
    storage = LocalStorage(str(tmp_path_factory.mktemp("analysis_storage")))
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-analysis"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"), TestClient(app) as client:
            r = client.post(
                "/v1/admin/tenants",
                json={"name": "AnalysisTenant", "plan": "free"},
                headers={"Authorization": "Bearer test-admin-analysis"},
            )
            assert r.status_code == 200, r.text
            tenant_id, api_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with ExitStack() as stack:
                for mod in _STORAGE_USERS:
                    stack.enter_context(patch(f"src.server.api.routers.{mod}.get_storage", return_value=storage))
                with TestClient(app) as client:
                    headers = {"Authorization": f"Bearer {api_key}"}
                    r = client.post("/v1/libraries", json={"name": "Footage", "root_path": "/Volumes/media-01/Footage"},
                                    headers=headers)
                    assert r.status_code == 200, r.text
                    yield client, headers, r.json()["library_id"], storage, tenant_id, tenant_url
        _engines.clear()


def _ingest(env, rel_path: str, media_type: str = "video", sha: str | None = None,
            facet: dict | None = None) -> str:
    client, headers, library_id, *_ = env
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": media_type,
            "width": "64", "height": "36", "lineage": ingest_made(sha)}
    if sha:
        data["exif"] = json.dumps({"sha256": sha})
    if facet is not None:
        data["video_facet"] = json.dumps(facet)
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _upload(env, asset_id: str, body: bytes = b"\x00\x00\x00\x18ftypmp42 analysis", content_type="video/mp4"):
    client, headers, *_ = env
    return client.post(
        f"/v1/assets/{asset_id}/artifacts/analysis_proxy",
        files={"file": ("analysis.mp4", io.BytesIO(body), content_type)},
        data={"lineage": made_json("analysis_proxy")},
        headers=headers,
    )


def _summary(env) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/repair-summary", params={"library_id": library_id}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _page(env, **params) -> list[dict]:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/page", params={"library_id": library_id, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------------------
# Upload and download
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_upload_stores_the_proxy_and_marks_the_asset(env) -> None:
    asset_id = _ingest(env, "up/a.mov")
    body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1000
    r = _upload(env, asset_id, body)
    assert r.status_code == 200, r.text
    assert "/analysis/" in r.json()["key"]
    assert r.json()["sha256"] == hashlib.sha256(body).hexdigest()

    storage = env[3]
    assert storage.abs_path(r.json()["key"]).read_bytes() == body
    [item] = [i for i in _page(env) if i["asset_id"] == asset_id]
    assert item["has_analysis_proxy"] is True


@pytest.mark.slow
def test_download_returns_the_file_with_ranges_and_an_etag(env) -> None:
    client, headers, *_ = env
    asset_id = _ingest(env, "dl/a.mov")
    body = bytes(range(256)) * 4
    sha = _upload(env, asset_id, body).json()["sha256"]

    r = client.get(f"/v1/assets/{asset_id}/artifacts/analysis_proxy", headers=headers)
    assert r.status_code == 200
    assert r.content == body
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["etag"] == f'"{sha}"'
    assert r.headers["accept-ranges"] == "bytes"

    part = client.get(f"/v1/assets/{asset_id}/artifacts/analysis_proxy",
                      headers={**headers, "Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.content == body[10:20]


@pytest.mark.slow
def test_download_before_render_is_not_ready(env) -> None:
    client, headers, *_ = env
    asset_id = _ingest(env, "dl/none.mov")
    r = client.get(f"/v1/assets/{asset_id}/artifacts/analysis_proxy", headers=headers)
    assert r.status_code == 404
    assert "artifact_not_ready" in r.text


@pytest.mark.slow
def test_photos_have_no_analysis_proxy(env) -> None:
    asset_id = _ingest(env, "photo/a.jpg", media_type="image")
    r = _upload(env, asset_id)
    assert r.status_code == 400
    assert "video" in r.text


@pytest.mark.slow
def test_upload_must_be_a_video_file(env) -> None:
    asset_id = _ingest(env, "type/a.mov")
    r = _upload(env, asset_id, b"not a video", content_type="image/jpeg")
    assert r.status_code == 400


@pytest.mark.slow
def test_analysis_proxies_have_their_own_size_ceiling(env) -> None:
    # Full-length proxies of long recordings are far bigger than the
    # 100 MB ceiling for previews and stills.
    client, headers, *_ = env
    asset_id = _ingest(env, "size/a.mov")
    with patch("src.server.api.routers.artifacts.MAX_UPLOAD_BYTES", 10):
        assert _upload(env, asset_id, b"y" * 50).status_code == 200
        r = client.post(
            f"/v1/assets/{asset_id}/artifacts/video_preview",
            files={"file": ("p.mp4", io.BytesIO(b"y" * 50), "video/mp4")},
            data={"lineage": made_json("video_preview")},
            headers=headers,
        )
        assert r.status_code == 413
    with patch.dict("src.server.api.routers.artifacts.MAX_UPLOAD_BYTES_BY_TYPE", {"analysis_proxy": 10}):
        assert _upload(env, asset_id, b"y" * 50).status_code == 413


@pytest.mark.slow
def test_unknown_asset_is_404(env) -> None:
    assert _upload(env, "ast_01JZZZZZZZZZZZZZZZZZZZZZZZ").status_code == 404


# ---------------------------------------------------------------------------
# What's missing
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_summary_and_page_count_videos_without_a_proxy(env) -> None:
    before = _summary(env)["missing_analysis_proxy"]
    video = _ingest(env, "missing/a.mov")
    _ingest(env, "missing/b.jpg", media_type="image")
    assert _summary(env)["missing_analysis_proxy"] == before + 1
    missing = {i["asset_id"] for i in _page(env, missing_analysis_proxy=True)}
    assert video in missing
    assert all(i["media_type"] == "video" for i in _page(env, missing_analysis_proxy=True))

    _upload(env, video)
    assert _summary(env)["missing_analysis_proxy"] == before
    assert video not in {i["asset_id"] for i in _page(env, missing_analysis_proxy=True)}


@pytest.mark.slow
def test_trashed_videos_are_not_counted(env) -> None:
    client, headers, *_ = env
    before = _summary(env)["missing_analysis_proxy"]
    video = _ingest(env, "trashed/a.mov")
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [video], "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _summary(env)["missing_analysis_proxy"] == before


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_changed_file_needs_a_new_proxy(env) -> None:
    video = _ingest(env, "changed/a.mov", sha="a" * 64)
    _upload(env, video)
    _ingest(env, "changed/a.mov", sha="a" * 64)  # rescan, same content
    assert next(i for i in _page(env) if i["asset_id"] == video)["has_analysis_proxy"] is True

    replaced = _ingest(env, "changed/a.mov", sha="b" * 64)  # another file there: a new clip (Robert, Oct 9)
    assert replaced != video
    assert next(i for i in _page(env) if i["asset_id"] == replaced)["has_analysis_proxy"] is False


@pytest.mark.slow
def test_deleting_for_good_removes_the_proxy_file(env) -> None:
    client, headers, *_ = env
    storage = env[3]
    video = _ingest(env, "purge/a.mov")
    key = _upload(env, video).json()["key"]
    assert storage.abs_path(key).exists()

    client.request("DELETE", "/v1/assets", json={"asset_ids": [video], "reason": "user"}, headers=headers)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [video], "remove_from_projects": True},
                       headers=headers)
    assert r.status_code == 200, r.text
    assert not storage.abs_path(key).exists()


@pytest.mark.slow
def test_cleanup_keeps_proxies_and_removes_orphans(env) -> None:
    import time

    from sqlmodel import Session

    from src.server.search.cleanup import run_cleanup_for_tenant

    *_, storage, tenant_id, tenant_url = env
    library_id = env[2]
    kept = [storage.abs_path(_upload(env, _ingest(env, f"clean/{n}.mov")).json()["key"]) for n in range(4)]
    orphan = kept[0].with_name("ast_01JORPHAN000000000000000000_gone.mp4")
    orphan.write_bytes(b"orphan")
    old = time.time() - 7200
    for path in [*kept, orphan]:
        os.utime(path, (old, old))

    engine = create_engine(tenant_url)
    with Session(engine) as session:
        result = run_cleanup_for_tenant(storage.abs_path("x").parent, tenant_id, session, dry_run=False)
    engine.dispose()

    assert all(p.exists() for p in kept)
    assert not orphan.exists()
    assert result.orphan_files >= 1
    assert library_id  # the files lived under this library


@pytest.mark.slow
def test_library_health_counts_videos_waiting_for_a_proxy(env) -> None:
    # The libraries page shows a library as pending until the brain renders.
    client, headers, library_id, *_ = env

    def pending() -> int:
        r = client.get("/v1/libraries/health", headers=headers)
        assert r.status_code == 200, r.text
        return next(row["pending"] for row in r.json() if row["library_id"] == library_id)

    before = pending()
    # Probed, length unknown: nothing else is missing, so only the proxy counts.
    # As a scan sends it: with its EXIF, so its capture facts count as read.
    video = _ingest(env, "health/a.mov", sha=os.urandom(32).hex(), facet={"video_codec": "h264"})
    assert pending() == before + 1
    _upload(env, video)
    assert pending() == before
