# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Tenant migration a3c9e5f1b7d2: Lumiverb's own files kept by their names
(ADR-024 phase 1 part 4).

- Its mapping is its own, and while src/producers/names.py RENAMED exists the
  two are the same.
- An account from before part 4 upgraded straight to the head: its open
  uploads and the missing-file reports waiting for upkeep, written out here
  as each is named after (a test that stays when part 5 removes RENAMED),
  and the CHECK on uploads taking only the names; down, every row as it was.
- Through today's code: an upload open and a missing-file report pending
  across a migration down and up are as they were, and the upload finishes
  and the repair repairs after.

Runs Alembic against throwaway databases only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_migration_lineage import _alembic

pytestmark = pytest.mark.slow

REPO = Path(__file__).resolve().parents[1]
BEFORE, AFTER = "f2a6c8e4b0d7", "a3c9e5f1b7d2"


def _migration():
    path = REPO / "migrations" / "tenant" / "versions" / "a3c9e5f1b7d2_files_named.py"
    spec = importlib.util.spec_from_file_location("files_named", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_migration_s_mapping_is_renamed():
    # Its mapping is its own (part 5 deletes RENAMED); while both exist they're the same.
    from src.producers.names import RENAMED
    from src.server import uploads

    m = _migration()
    assert dict(RENAMED["file"]) == m.FILES
    assert {m.FILES[old] for old in m.UPLOADED} == set(uploads.KINDS)


# ---------------------------------------------------------------------------
# An account from before part 4, upgraded straight to the head
# ---------------------------------------------------------------------------

UPLOADS = [("upl_v", "video_preview", "lumiverb.video.preview"),
           ("upl_a", "analysis_proxy", "lumiverb.video.analysis_proxy")]
# Each report as it was, and as it's kept after (one that isn't a report stays).
REPORTS = [("ast_x:proxy", "ast_x:lumiverb.preview"),
           ("ast_x:thumbnail", "ast_x:lumiverb.preview.thumbnail"),
           ("ast_y:video_preview", "ast_y:lumiverb.video.preview"),
           ("ast_y:analysis_proxy", "ast_y:lumiverb.video.analysis_proxy"),
           ("ast_z:transcript", "ast_z:transcript"),  # no file of that kind: as it was
           ("not a report", "not a report")]


def _fresh(pg) -> str:
    url = _ensure_psycopg2(pg.get_connection_url())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    return url


def _kept(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {"uploads": sorted(tuple(r) for r in conn.execute(text("SELECT upload_id, artifact FROM uploads"))),
                    "reports": json.loads(conn.execute(text(
                        "SELECT value FROM system_metadata WHERE key = 'artifacts.missing'")).scalar())}
    finally:
        engine.dispose()


def _upload(conn, upload_id: str, asset_id: str, artifact: str) -> None:
    conn.execute(text("INSERT INTO uploads (upload_id, asset_id, artifact, size, sha256, lineage, received, created_at,"
                      " expires_at) VALUES (:u, :a, :b, 10, :sha, CAST(:l AS jsonb), 0, now(), now() + interval '1 day')"),
                 {"u": upload_id, "a": asset_id, "b": artifact, "sha": hashlib.sha256(upload_id.encode()).hexdigest(),
                  "l": json.dumps({"producer": "lumiverb.video.preview", "version": "1", "settings_hash": "h"})})


def test_an_account_from_before_part_4_upgraded_straight_to_head() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _fresh(pg)
        _alembic(url, "upgrade", BEFORE)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
        with Session(engine) as session:
            video = make_clip(session, "lib_a", "v.mov", media_type="video")
            session.commit()
        with engine.begin() as conn:
            for upload_id, old, _ in UPLOADS:
                _upload(conn, upload_id, video, old)
            conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('artifacts.missing', :v,"
                              " now())"), {"v": json.dumps([old for old, _ in REPORTS] + [3])})
        before = _kept(url)
        _alembic(url, "upgrade", "head")
        after = _kept(url)
        assert after == {"uploads": sorted((u, new) for u, _, new in UPLOADS),
                         "reports": [new for _, new in REPORTS] + [3]}
        with pytest.raises(IntegrityError, match="uploads_artifact"), engine.begin() as conn:
            _upload(conn, "upl_old", video, "analysis_proxy")  # the CHECK takes the names only

        _alembic(url, "downgrade", BEFORE)
        assert _kept(url) == before, "down, every row as it was"
        with pytest.raises(IntegrityError, match="uploads_artifact"), engine.begin() as conn:
            _upload(conn, "upl_new", video, "lumiverb.video.analysis_proxy")  # and the old CHECK back
        _alembic(url, "upgrade", "head")
        assert _kept(url) == after, "and up again the same"
        engine.dispose()


# ---------------------------------------------------------------------------
# Through today's code: an open upload and a pending repair, down and up
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    """The server's own folder for this test: where uploads wait."""
    from src.server.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()


def test_an_open_upload_and_a_pending_repair_go_down_and_up_and_finish(env, data_dir):
    from src.producers.names import ANALYSIS_PROXY, PREVIEW
    from tests.test_lineage_api import _ingest_with, _sha, _state, _want
    from tests.test_reconciler import _library
    from tests.videos import playable

    lib = _library(env, "FilesNamed")
    client, headers, _, storage, _, url = lib
    video = _ingest_with(lib, "v.mov", vsha := _sha(), media_type="video")
    data = playable(2.0)
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": ANALYSIS_PROXY, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": _want(lib, ANALYSIS_PROXY, vsha)})
    assert r.status_code == 200 and r.json()["artifact"] == ANALYSIS_PROXY, r.text
    upload = r.json()
    photo = _ingest_with(lib, "p.jpg", _sha())
    key = client.get(f"/v1/assets/{photo}", headers=headers).json()["proxy_key"]
    storage.abs_path(key).unlink()  # its preview's file gone: the GET reports it
    r = client.get(f"/v1/assets/{photo}/artifacts/{PREVIEW}", headers=headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "artifact_missing", r.text
    assert f"{photo}:{PREVIEW}" in _kept(url)["reports"]

    _alembic(url, "downgrade", BEFORE)
    down = _kept(url)
    assert (upload["upload_id"], "analysis_proxy") in down["uploads"] and f"{photo}:proxy" in down["reports"]
    _alembic(url, "upgrade", "head")
    up = _kept(url)
    assert (upload["upload_id"], ANALYSIS_PROXY) in up["uploads"] and f"{photo}:{PREVIEW}" in up["reports"]

    # The upload finishes after, its analysis proxy as it would have been.
    r = client.put(upload["chunk_url"], params={"offset": 0}, content=data,
                   headers={**headers, "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    r = client.post(f"{upload['chunk_url']}/finish", headers=headers)
    assert r.status_code == 200, r.text
    assert _state(lib, video, ANALYSIS_PROXY) == "current"
    # And upkeep repairs what was reported: the preview's key cleared for its producer to make again.
    r = client.post("/v1/upkeep", headers=headers)
    assert r.status_code == 200 and r.json()["missing_files"]["cleared"] >= 1, r.text
    assert client.get(f"/v1/assets/{photo}", headers=headers).json()["proxy_key"] is None


def test_a_report_from_before_part_4_is_still_repaired(env):
    # One an older API worker wrote as the upgrade went on (the old kind): taken until part 5.
    from src.server import missing_artifacts
    from src.server.database import get_engine_for_url
    from tests.test_lineage_api import _ingest_with, _sha
    from tests.test_reconciler import _library

    lib = _library(env, "OldReport")
    client, headers, _, storage, _, url = lib
    photo = _ingest_with(lib, "p.jpg", _sha())
    storage.abs_path(client.get(f"/v1/assets/{photo}", headers=headers).json()["proxy_key"]).unlink()
    with Session(get_engine_for_url(url)) as session:
        session.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('artifacts.missing', :v, now())"
                             " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                        {"v": json.dumps([f"{photo}:proxy"])})
        session.commit()
        assert missing_artifacts.repair(session)["cleared"] == 1
    assert client.get(f"/v1/assets/{photo}", headers=headers).json()["proxy_key"] is None
