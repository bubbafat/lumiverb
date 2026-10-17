# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Uploads in chunks that survive a dropped connection (ADR-018 decision 9,
phase 5; src/server/uploads.py, routers/uploads.py, src/processing/uploads.py).

Against the real API and database: start, chunks at their offsets, finish;
a start sent again picks up the same upload, a chunk sent again adds
nothing, a resume with another chunk size goes on from the same byte; what
isn't the file said, or isn't a video a browser plays, is dropped; expired
uploads go with upkeep. The client sends a render the same way the
scheduler does.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from src.server import uploads
from tests.machine_lineage import made
from tests.test_analysis_proxy_api import (  # noqa: F401 — the shared server fixture
    _analysis_proxy_key,
    _ingest,
    env,
)
from tests.test_lineage_api import _db
from tests.videos import made_with, playable


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch) -> Path:
    """The server's own folder for this test: where uploads wait."""
    from src.server.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def _start(env, asset_id: str, data: bytes, artifact: str = "analysis_proxy", **body) -> dict:
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{asset_id}/uploads", headers=headers, json={
        "artifact": artifact, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made(artifact), **body})
    assert r.status_code == 200, r.text
    return r.json()


def _put(env, upload: dict, data: bytes, offset: int, length: int):
    client, headers, *_ = env
    return client.put(upload["chunk_url"], params={"offset": offset}, content=data[offset:offset + length],
                      headers={**headers, "Content-Type": "application/octet-stream"})


def _finish(env, upload: dict):
    client, headers, *_ = env
    return client.post(f"{upload['chunk_url']}/finish", headers=headers)


def _error(r, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code, r.text
    return r.json()["error"]


@pytest.mark.slow
def test_a_file_in_chunks_is_kept_as_one_upload_would_keep_it(env, data_dir):
    client, headers, *_, storage, _, _ = env
    asset_id = _ingest(env, "chunks/a.mov")
    data = playable(3.0)
    upload = _start(env, asset_id, data)
    assert upload["received"] == 0 and upload["size"] == len(data) and not upload["finished"]
    assert upload["chunk_url"] == f"/v1/uploads/{upload['upload_id']}"
    assert upload["chunk_size"] <= upload["max_chunk"]
    step = 20_000
    for offset in range(0, len(data), step):
        r = _put(env, upload, data, offset, step)
        assert r.status_code == 200 and r.json() == {"received": min(offset + step, len(data))}, r.text
    r = _finish(env, upload)
    assert r.status_code == 200, r.text
    kept = r.json()
    assert kept["duration_sec"] == pytest.approx(3.0, abs=0.1)
    assert _analysis_proxy_key(env, asset_id) == kept["key"]
    assert hashlib.sha256(storage.abs_path(kept["key"]).read_bytes()).hexdigest() == kept["sha256"]
    with _db(env) as s:  # how it was made, as the start said
        assert s.execute(text("SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = 'analysis_proxy'"),
                         {"a": asset_id}).scalar() == made("analysis_proxy")["producer"]
    assert not any(data_dir.rglob("*.part")), "its bytes go once it's kept"
    assert _finish(env, upload).json() == kept  # sent again: the answer the first got
    status = client.get(upload["chunk_url"], headers=headers).json()
    assert status["finished"] and status["received"] == len(data)
    _error(_put(env, upload, data, 0, 10), 409, "upload_finished")


@pytest.mark.slow
def test_a_chunk_sent_again_adds_nothing_and_a_gap_is_refused(env):
    asset_id = _ingest(env, "chunks/again.mov")
    data = playable(2.0)
    upload = _start(env, asset_id, data)
    assert _put(env, upload, data, 0, 30_000).json() == {"received": 30_000}
    assert _put(env, upload, data, 0, 30_000).json() == {"received": 30_000}  # its answer was lost: sent again
    assert _put(env, upload, data, 10_000, 30_000).json() == {"received": 40_000}  # half new
    error = _error(_put(env, upload, data, 50_000, 10_000), 409, "upload_gap")
    assert error["details"] == {"received": 40_000}
    assert _put(env, upload, data, 40_000, len(data)).json() == {"received": len(data)}
    assert _finish(env, upload).status_code == 200


@pytest.mark.slow
def test_starting_the_same_file_again_resumes_it_with_any_chunk_size(env):
    # A client that restarted holds nothing: it starts the file again and goes on where the server is.
    asset_id = _ingest(env, "chunks/resume.mov")
    data = playable(2.0)
    first = _start(env, asset_id, data)
    _put(env, first, data, 0, 16_384)
    again = _start(env, asset_id, data)
    assert again["upload_id"] == first["upload_id"] and again["received"] == 16_384
    for offset in range(16_384, len(data), 32_768):  # bigger chunks now, from the same byte
        assert _put(env, again, data, offset, 32_768).status_code == 200
    assert _finish(env, again).status_code == 200
    other = _start(env, asset_id, data)  # the same file once more, after it's kept: a new upload
    assert other["upload_id"] != first["upload_id"] and other["received"] == 0


@pytest.mark.slow
def test_finishing_too_soon_or_with_other_bytes(env, data_dir):
    asset_id = _ingest(env, "chunks/wrong.mov")
    data = playable(2.0)
    upload = _start(env, asset_id, data)
    _put(env, upload, data, 0, 1000)
    error = _error(_finish(env, upload), 409, "upload_incomplete")
    assert error["details"] == {"received": 1000, "size": len(data)}
    damaged = data[:1000] + bytes(len(data) - 1000)  # what arrived isn't the file said
    _put(env, upload, damaged, 1000, len(data))
    _error(_finish(env, upload), 422, "upload_damaged")
    client, headers, *_ = env
    assert client.get(upload["chunk_url"], headers=headers).status_code == 404  # dropped: start again
    assert not any(data_dir.rglob("*.part"))
    assert _analysis_proxy_key(env, asset_id) is None


@pytest.mark.slow
def test_a_video_no_browser_plays_is_dropped_at_the_finish(env):
    asset_id = _ingest(env, "chunks/hevc.mov")
    data = made_with("-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error", "-tag:v", "hvc1")
    upload = _start(env, asset_id, data, artifact="video_preview")
    _put(env, upload, data, 0, len(data))
    error = _error(_finish(env, upload), 422, "video_not_playable")
    assert any("not H.264" in p for p in error["details"]["problems"])
    client, headers, *_ = env
    assert client.get(upload["chunk_url"], headers=headers).status_code == 404


@pytest.mark.slow
def test_what_an_upload_can_be(env):
    client, headers, *_ = env
    video = _ingest(env, "chunks/rules.mov")
    photo = _ingest(env, "chunks/rules.jpg", media_type="image")
    data = playable()
    body = {"artifact": "analysis_proxy", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "lineage": made("analysis_proxy")}
    assert client.post(f"/v1/assets/{photo}/uploads", json=body, headers=headers).status_code == 400
    assert client.post("/v1/assets/ast_nope/uploads", json=body, headers=headers).status_code == 404
    assert client.post(f"/v1/assets/{video}/uploads", json={**body, "artifact": "proxy"},
                       headers=headers).status_code == 422  # pictures are one request
    assert client.post(f"/v1/assets/{video}/uploads", json={**body, "sha256": "ABC"}, headers=headers).status_code == 422
    _error(client.post(f"/v1/assets/{video}/uploads", json={**body, "lineage": None}, headers=headers),
           422, "lineage_required")
    upload = _start(env, video, data)
    r = _put(env, upload, data + b"more", 0, len(data) + 4)
    assert r.status_code == 413  # past its size
    assert client.get("/v1/uploads/upl_nope", headers=headers).status_code == 404
    assert client.delete(upload["chunk_url"], headers=headers).status_code == 204
    assert client.get(upload["chunk_url"], headers=headers).status_code == 404


@pytest.mark.slow
def test_a_viewer_sends_nothing(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, *_ = env
    video = _ingest(env, "chunks/viewer.mov")
    data = playable()
    r = client.post(f"/v1/assets/{video}/uploads", headers=_key_with_role(env, "viewer"), json={
        "artifact": "analysis_proxy", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made("analysis_proxy")})
    assert r.status_code == 403


@pytest.mark.slow
def test_upkeep_deletes_uploads_a_day_after_their_last_chunk(env, data_dir):
    client, headers, *_, tenant_id, _ = env
    video = _ingest(env, "chunks/expired.mov")
    data = playable()
    upload = _start(env, video, data)
    _put(env, upload, data, 0, 1000)
    stray = uploads.folder(tenant_id) / "upl_01STRAY.part"  # a crash's, claimed by no upload
    stray.write_bytes(b"x")
    os.utime(stray, (0, 0))
    with _db(env) as s:
        s.execute(text("UPDATE uploads SET expires_at = now() - interval '1 second' WHERE upload_id = :u"),
                  {"u": upload["upload_id"]})
        s.commit()
    r = client.post("/v1/upkeep", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["expired_uploads"] == {"deleted": 1}
    assert client.get(upload["chunk_url"], headers=headers).status_code == 404
    assert not uploads.part(tenant_id, upload["upload_id"]).exists() and not stray.exists()


@pytest.mark.slow
def test_the_client_sends_a_render_in_chunks_as_the_scheduler_does(env, tmp_path):
    from fastapi.testclient import TestClient

    from src.processing.api import ApiClient
    from src.processing.uploads import send
    from src.server.api.main import app

    client, headers, *_, storage, _, _ = env
    asset_id = _ingest(env, "chunks/client.mov")
    render = tmp_path / "analysis.mp4"
    render.write_bytes(playable(2.0))
    api = ApiClient("http://testserver", "unused")
    api._client = TestClient(app, headers=headers)
    kept = send(api, asset_id, "analysis_proxy", render, made("analysis_proxy"), chunk_size=10_000)
    assert _analysis_proxy_key(env, asset_id) == kept["key"]
    assert kept["duration_sec"] == pytest.approx(2.0, abs=0.1)
    again = send(api, asset_id, "analysis_proxy", render, made("analysis_proxy"))  # the same render sent again
    assert again == kept  # a new upload of it, kept the same
