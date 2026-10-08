# ruff: noqa: F811 — pytest fixtures are named as parameters
"""OCR has a table of its own (ADR-016 phase 3, piece 3: one producer per artifact).

It used to live inside the description's row, so describing a clip again
wiped it, and OCR couldn't run before a description existed. Now each is
its own artifact: OCR needs only the clip's proxy, a new description
leaves it alone, and it's read from one place by the detail, search and
the reconciler.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _describe, _due, _library, _summary


def _ocr(env, clip: str, words: str, sha: str | None = None):
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": words, "lineage": _want(env, "ocr", sha)},
                    headers=headers)
    assert r.status_code == 200, r.text
    return r


def _detail(env, clip: str) -> dict:
    client, headers, *_ = env
    r = client.get(f"/v1/assets/{clip}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.slow
def test_ocr_doesnt_wait_for_a_description(env):
    lib = _library(env, "OcrFirst")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    assert _due(lib, "missing_ocr") == [clip]  # handed out right away
    _ocr(lib, clip, "EXIT", sha)
    assert _detail(env, clip)["ocr_text"] == "EXIT"
    assert _due(lib, "missing_ocr") == []
    assert _counts(lib, "ocr")["current"] == 1


@pytest.mark.slow
def test_describing_again_leaves_ocr_alone(env):
    client, headers, *_ = env
    lib = _library(env, "OcrKept")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _ocr(lib, clip, "NO PARKING", sha)
    _describe(lib, clip, sha)
    # Another model's description, and the same model's again.
    client.post(f"/v1/assets/{clip}/vision", json={"model_id": "other", "description": "a sign"}, headers=headers)
    _describe(lib, clip, sha)
    assert _detail(env, clip)["ocr_text"] == "NO PARKING"
    assert _due(lib, "missing_ocr") == []


@pytest.mark.slow
def test_no_text_is_an_answer_too(env):
    lib = _library(env, "OcrEmpty")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _ocr(lib, clip, "", sha)
    assert _detail(env, clip).get("ocr_text") in ("", None)
    assert _due(lib, "missing_ocr") == []


@pytest.mark.slow
def test_a_batch_doesnt_need_descriptions_either(env):
    client, headers, *_ = env
    lib = _library(env, "OcrBatch")
    clips = [_ingest_with(lib, f"{i}.jpg", _sha(), None) for i in range(2)]
    r = client.post("/v1/assets/batch-ocr", json={
        "items": [{"asset_id": c, "ocr_text": f"TEXT {i}"} for i, c in enumerate(clips)] + [
            {"asset_id": "ast_nope", "ocr_text": "x"}],
        "lineage": _want(env, "ocr", None)}, headers=headers)
    assert r.status_code == 200, r.text
    assert (r.json()["updated"], r.json()["skipped"]) == (2, 1)
    assert [_detail(env, c)["ocr_text"] for c in clips] == ["TEXT 0", "TEXT 1"]
    assert _summary(lib)["missing_ocr"] == 0


@pytest.mark.slow
def test_the_postgres_search_finds_words_in_an_image(env):
    from src.server.search.postgres_search import search_assets

    lib = _library(env, "OcrSearch")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    _ocr(lib, clip, "Grand Central Terminal", None)
    with _db(env) as s:
        hits = search_assets(s, lib[2], "central terminal")
    assert [h["asset_id"] for h in hits] == [clip]


@pytest.mark.slow
def test_the_search_sweep_covers_every_clip_and_carries_its_ocr(env):
    from src.server.search.sync import run_search_sync_sweep

    lib = _library(env, "OcrSweep")
    undescribed = _ingest_with(lib, "a.jpg", _sha(), None)
    video = _ingest_with(lib, "b.mov", _sha(), None, media_type="video")
    _ocr(lib, undescribed, "PLATFORM 9", None)
    qw = MagicMock()
    qw.ensure_tenant_index.return_value = False  # the index was there already
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = NULL WHERE library_id = :l"), {"l": lib[2]})
        s.commit()
        with patch("src.server.search.sync._get_quickwit", return_value=qw):
            run_search_sync_sweep(s, tenant_id=env[4])
    docs = {d["asset_id"]: d for call in qw.method_calls for arg in call.args if isinstance(arg, list)
            for d in arg if isinstance(d, dict) and "asset_id" in d}
    assert undescribed in docs and video in docs
    assert docs[undescribed]["ocr_text"] == "PLATFORM 9"

    # OCR newer than the clip's search document brings it back into the sweep.
    qw = MagicMock()
    qw.ensure_tenant_index.return_value = False  # the index was there already
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))  # the sweep is tenant-wide
        s.execute(text("UPDATE asset_ocr SET text = 'PLATFORM 10', generated_at = now() + interval '1 minute'"
                       " WHERE asset_id = :a"), {"a": undescribed})
        s.commit()
        with patch("src.server.search.sync._get_quickwit", return_value=qw):
            run_search_sync_sweep(s, tenant_id=env[4])
    docs = {d["asset_id"]: d for call in qw.method_calls for arg in call.args if isinstance(arg, list)
            for d in arg if isinstance(d, dict) and "asset_id" in d}
    assert set(docs) == {undescribed} and docs[undescribed]["ocr_text"] == "PLATFORM 10"


@pytest.mark.slow
def test_deleting_a_clip_for_good_takes_its_ocr(env):
    client, headers, *_ = env
    lib = _library(env, "OcrPurge")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    _ocr(lib, clip, "GONE", None)
    assert client.request("DELETE", "/v1/assets", json={"asset_ids": [clip], "reason": "user"},
                          headers=headers).status_code in (200, 204)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clip]}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(env) as s:
        assert s.execute(text("SELECT count(*) FROM asset_ocr WHERE asset_id = :a"), {"a": clip}).scalar() == 0


# ---------------------------------------------------------------------------
# The migration moves OCR out of the descriptions
# ---------------------------------------------------------------------------


def test_the_migration_moves_ocr_into_its_table_and_back():
    from sqlalchemy import create_engine
    from testcontainers.postgres import PostgresContainer

    from tests.conftest import PG_IMAGE, _ensure_psycopg2
    from tests.test_migration_lineage import _alembic

    before, after = "d9f3a6b1c2e8", "e1a2b3c4d5f6"
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic(url, "upgrade", before)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib', 'lib', '/lib', 'active', now(), now())"))
            for aid in ("a_text", "a_none", "a_rewritten", "a_never"):
                conn.execute(text("INSERT INTO assets (asset_id, library_id, rel_path, file_size, media_type, status,"
                                  " availability, created_at, updated_at) VALUES (:a, 'lib', :a, 1, 'image',"
                                  " 'proxy_ready', 'online', now(), now())"), {"a": aid})

            def meta(aid, model, data, at):
                conn.execute(text("INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version,"
                                  " generated_at, data) VALUES (:i, :a, :m, '1', :t, CAST(:d AS jsonb))"),
                             {"i": f"{aid}-{model}", "a": aid, "m": model, "t": at, "d": data})

            meta("a_text", "qwen", '{"description": "d", "ocr_text": "EXIT", "has_text": true}', "2026-10-01")
            meta("a_none", "qwen", '{"description": "d", "has_text": false}', "2026-10-01")
            # Described again by another model after OCR: the older row has the OCR.
            meta("a_rewritten", "qwen", '{"description": "d", "ocr_text": "STOP", "has_text": true}', "2026-10-01")
            meta("a_rewritten", "llava", '{"description": "d2"}', "2026-10-02")
            meta("a_never", "qwen", '{"description": "d"}', "2026-10-01")

        _alembic(url, "upgrade", after)
        with engine.connect() as conn:
            ocr = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(text(
                "SELECT asset_id, text, has_text, model_id FROM asset_ocr"))}
            leftover = conn.execute(text(
                "SELECT count(*) FROM asset_metadata WHERE data ? 'ocr_text' OR data ? 'has_text'")).scalar()
        assert ocr == {"a_text": ("EXIT", True, "qwen"), "a_none": ("", False, "qwen"),
                       "a_rewritten": ("STOP", True, "qwen")}
        assert leftover == 0  # one place

        _alembic(url, "downgrade", before)
        with engine.connect() as conn:
            back = dict(conn.execute(text(
                "SELECT DISTINCT ON (asset_id) asset_id, data->>'ocr_text' FROM asset_metadata"
                " WHERE data ? 'has_text' ORDER BY asset_id, generated_at DESC")).all())
            assert conn.execute(text("SELECT to_regclass('asset_ocr')")).scalar() is None
        assert back == {"a_text": "EXIT", "a_none": None, "a_rewritten": "STOP"}
        engine.dispose()
