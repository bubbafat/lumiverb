# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Search sync against a real Quickwit: what a re-sync leaves searchable.

Quickwit doesn't upsert, so every re-sync deletes a document's old version
before ingesting the new one, or the old words stay searchable for good. And
what's deleted is only what goes in again: a clip's other documents (its
scenes, its transcript) stay. Quickwit deletes in the background, and only
from mature splits (48 h old by default, or merged): these tests' indexes
mature in seconds, and each test waits until a document it replaced is gone
before looking at the rest.

Opt-in with the other real-Quickwit tests: `uv run pytest -m quickwit`.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
from sqlalchemy import text

from src.server.models.tenant import AssetMetadata, VideoScene
from src.server.search.sync import (
    _transcript_documents,
    build_scene_document,
    run_search_sync_sweep,
    try_sync_asset,
    try_sync_scene,
)
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_quickwit_real import (  # noqa: F401
    _asset,
    _ids,
    _search,
    quickwit_url,
    qw,
)
from tests.test_reconciler import _library

pytestmark = pytest.mark.quickwit


def _found(tenant_id: str, library_id: str, word: str) -> set[str]:
    scores, _, source = _search(tenant_id, library_id, word)
    assert source == "quickwit"
    return set(scores)


def _maturing_indexes(qw, tenant_id: str) -> None:
    """The tenant's three indexes as the server makes them, but whose splits
    mature (so take deletes) in seconds."""
    for index_id, schema in ((qw.tenant_index_id(tenant_id), qw._schema_path()),
                             (qw.tenant_scene_index_id(tenant_id), qw._scene_schema_path()),
                             (qw.tenant_transcript_index_id(tenant_id), qw._transcript_schema_path())):
        data = json.loads(schema.read_text(encoding="utf-8"))
        data["index_id"] = index_id
        data["indexing_settings"]["merge_policy"] = {"type": "stable_log", "maturation_period": "5s"}
        r = requests.post(f"{qw._base_url}/api/v1/indexes", json=data, timeout=10)
        assert r.status_code == 200, r.text
        qw._wait_until_ready(index_id)


def _until(check, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


def _sync(asset, meta, corrections, tenant_id, qw) -> None:
    with patch("src.server.repository.tenant.AssetOcrRepository.text_for", return_value=""), \
         patch("src.server.repository.corrections.CorrectionsRepository.get", return_value=corrections):
        assert try_sync_asset(MagicMock(), asset, meta, tenant_id=tenant_id, quickwit=qw)


def test_inline_asset_sync_replaces_the_old_document_and_keeps_the_clip_s_others(qw) -> None:
    tenant_id, library_id = _ids()
    _maturing_indexes(qw, tenant_id)
    asset = _asset(library_id, "video", rel_path="x/clip.mov", transcript_language="en")
    meta = AssetMetadata(metadata_id="m1", asset_id=asset.asset_id, model_id="m", model_version="1",
                         generated_at=datetime.now(UTC), data={"description": "zorblax at the beach", "tags": []})
    _sync(asset, meta, None, tenant_id, qw)
    scene = VideoScene(scene_id=f"scn_{uuid.uuid4().hex[:12]}", asset_id=asset.asset_id, scene_index=0,
                       start_ms=0, end_ms=1000, rep_frame_ms=500, description="a trawler unloading", tags=[])
    qw.ingest_tenant_scene_documents(tenant_id, [build_scene_document(scene, asset)])
    qw.ingest_tenant_transcript_documents(
        tenant_id, _transcript_documents(asset, "1\n00:00:01,000 --> 00:00:02,000\nWatch the cormorants.\n"))
    assert asset.asset_id in _found(tenant_id, library_id, "zorblax")

    # A person rewrites the description; the correction route syncs again.
    _sync(asset, meta, {"description": "quillfeather at the beach", "ocr_text": None, "tags": None}, tenant_id, qw)

    assert _until(lambda: asset.asset_id not in _found(tenant_id, library_id, "zorblax")), \
        "the old description is still searchable after the re-sync"
    assert asset.asset_id in _found(tenant_id, library_id, "quillfeather")
    assert asset.asset_id in _found(tenant_id, library_id, "trawler")  # its scene stays
    assert asset.asset_id in _found(tenant_id, library_id, "cormorants")  # and its transcript


def test_inline_scene_sync_replaces_that_scene_only(qw) -> None:
    tenant_id, library_id = _ids()
    _maturing_indexes(qw, tenant_id)
    asset = _asset(library_id, "video", rel_path="x/scenes.mov")
    one, two = (VideoScene(scene_id=f"scn_{uuid.uuid4().hex[:12]}", asset_id=asset.asset_id, scene_index=i,
                           start_ms=i * 1000, end_ms=i * 1000 + 999, rep_frame_ms=i * 1000, description=d, tags=[])
                for i, d in enumerate(("a pelican on a piling", "a heron in the reeds")))
    for scene in (one, two):
        assert try_sync_scene(MagicMock(), scene, asset, tenant_id=tenant_id, quickwit=qw)
    one.description = "a cormorant drying its wings"  # described again
    assert try_sync_scene(MagicMock(), one, asset, tenant_id=tenant_id, quickwit=qw)

    assert _until(lambda: asset.asset_id not in _found(tenant_id, library_id, "pelican")), \
        "the scene's old description is still searchable after the re-sync"
    assert asset.asset_id in _found(tenant_id, library_id, "cormorant")
    assert asset.asset_id in _found(tenant_id, library_id, "heron")  # the clip's other scene stays


@pytest.mark.slow
def test_the_scene_sweep_keeps_a_clip_s_synced_scenes(env, qw) -> None:
    tenant_id = env[4]
    lib = _library(env, "SceneSweepReal")
    library_id = lib[2]
    vid = _ingest_with(lib, "v.mov", _sha(), None, media_type="video")
    _maturing_indexes(qw, tenant_id)
    words = ["quarvel", "brindlo", "zastrine", "mopquill", "tervalon"]
    with _db(env) as s:
        for i, word in enumerate(words):
            s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms,"
                           " rep_frame_ms, description, tags, created_at)"
                           " VALUES (:id, :a, :i, :st, :en, :st, :d, '[]', now() - interval '1 hour')"),
                      {"id": f"scn_{vid}_{i}", "a": vid, "i": i, "st": i * 1000, "en": i * 1000 + 999,
                       "d": f"a {word} by the water"})
        s.commit()
        assert run_search_sync_sweep(s, tenant_id=tenant_id)["scenes_synced"] >= 5  # all five go in
        assert _until(lambda: vid in _found(tenant_id, library_id, "quarvel"))

        # Scene 4 is described again and its inline sync failed: only it is stale.
        s.execute(text("UPDATE video_scenes SET description = 'a glimmerfax by the water', search_synced_at = NULL"
                       " WHERE scene_id = :s"), {"s": f"scn_{vid}_4"})
        s.commit()
        run_search_sync_sweep(s, tenant_id=tenant_id)

        assert _until(lambda: vid not in _found(tenant_id, library_id, "tervalon")), \
            "scene 4's old description is still searchable"
        assert vid in _found(tenant_id, library_id, "glimmerfax")
        for word in words[:4]:
            assert vid in _found(tenant_id, library_id, word), f"the sweep dropped the synced scene with {word!r}"
        assert s.execute(text("SELECT count(*) FROM video_scenes WHERE asset_id = :a AND search_synced_at IS NULL"),
                         {"a": vid}).scalar() == 0
