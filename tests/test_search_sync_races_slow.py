# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What the search sync sweep marks synced: never a write it didn't index.

A description, OCR or correction saved while the sweep runs is newer than the
sweep's start; a write that cleared the clip's sync time (a note, a move)
stays cleared. And a scene described again is stale until search has it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library


def _sweep_while(env, session, write_meanwhile) -> MagicMock:
    """Run the sweep; a write commits in its own transaction just as the
    sweep deletes the old asset documents (its rows were read, its documents
    built). Returns the Quickwit mock."""
    from src.server.search.sync import run_search_sync_sweep

    def write(*_a, **_k):
        with _db(env) as other:
            write_meanwhile(other)
            other.commit()

    qw = MagicMock()
    qw.ensure_tenant_index.return_value = False
    qw.ensure_tenant_scene_index.return_value = False
    qw.ensure_tenant_transcript_index.return_value = False
    qw.delete_asset_index_documents_by_asset_ids.side_effect = write
    with patch("src.server.search.sync._get_quickwit", return_value=qw):
        run_search_sync_sweep(session, tenant_id=env[4])
    return qw


def _stale(session, asset_id: str) -> bool:
    from src.server.search.sync import STALE_SEARCH

    return bool(session.execute(
        text(f"SELECT count(*) FROM active_assets a WHERE a.asset_id = :a AND {STALE_SEARCH}"),
        {"a": asset_id}).scalar())


def _save(other, write: str, clip: str) -> None:
    """A write as the API saves it: a vision or OCR save whose inline sync
    failed (the real repositories, so their own clock and locking), or a
    note (PUT /note clears the sync time)."""
    from src.server.repository.tenant import AssetMetadataRepository, AssetOcrRepository

    if write == "description":
        AssetMetadataRepository(other).upsert(clip, "m", "1", {"description": "new words", "tags": []})
    elif write == "ocr":
        AssetOcrRepository(other).upsert(clip, "new words", "m")
    else:
        other.execute(text("UPDATE assets SET note = 'new words', search_synced_at = NULL WHERE asset_id = :a"),
                      {"a": clip})


@pytest.mark.slow
@pytest.mark.parametrize("write", ["description", "ocr", "note"])
def test_a_write_during_the_sweep_stays_stale(env, write):
    lib = _library(env, f"SweepRace-{write}")
    clip = _ingest_with(lib, f"{write}.jpg", _sha(), None)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))
        s.execute(text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = :a"), {"a": clip})
        s.commit()

        qw = _sweep_while(env, s, lambda other: _save(other, write, clip))
        sent = [d for d in qw.ingest_tenant_documents.call_args.args[1] if d["asset_id"] == clip]
        assert len(sent) == 1 and "new words" not in json.dumps(sent[0])
        assert _stale(s, clip), "the sweep marked synced a write it never indexed"


@pytest.mark.slow
@pytest.mark.parametrize("write", ["description", "ocr"])
def test_a_save_timed_before_the_sweep_began_and_committed_after_it_read_stays_stale(env, write):
    """A save takes its time, then commits: a sweep that begins in between
    and reads before the commit is later than the save's time, yet never saw
    it. Its time can't say it's newer; its row can (it's no longer as the
    sweep read it). The save's clock is put a minute back to open the gap,
    whatever the machine's speed or clocks."""
    from src.server.repository import tenant

    lib = _library(env, f"SweepGap-{write}")
    clip = _ingest_with(lib, f"gap-{write}.jpg", _sha(), None)
    real_utcnow = tenant.utcnow
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))
        s.execute(text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = :a"), {"a": clip})
        s.commit()

        def save_timed_earlier(other):
            with patch.object(tenant, "utcnow", lambda: real_utcnow() - timedelta(minutes=1)):
                _save(other, write, clip)

        qw = _sweep_while(env, s, save_timed_earlier)
        sent = [d for d in qw.ingest_tenant_documents.call_args.args[1] if d["asset_id"] == clip]
        assert len(sent) == 1 and "new words" not in json.dumps(sent[0]), "the sweep read before the save"
        assert _stale(s, clip), "the sweep marked synced a save it never indexed"


@pytest.mark.slow
def test_a_scene_described_again_is_stale_until_search_has_it(env):
    from src.server.repository.tenant import VideoSceneRepository

    lib = _library(env, "SceneRedo")
    vid = _ingest_with(lib, "w.mov", _sha(), None, media_type="video")
    scene_id = f"scn_{vid}_0"
    with _db(env) as s:
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " description, tags, created_at, search_synced_at)"
                       " VALUES (:id, :a, 0, 0, 999, 0, 'old words', '[]', now() - interval '1 hour', now())"),
                  {"id": scene_id, "a": vid})
        s.commit()
        VideoSceneRepository(s).update_vision(scene_id, "m2", "1", "a brand new description", [])
        assert s.execute(text("SELECT search_synced_at IS NULL FROM video_scenes WHERE scene_id = :s"),
                         {"s": scene_id}).scalar()


@pytest.mark.slow
def test_after_a_restore_the_sweep_remakes_the_indexes_then_reindexes_everything(env):
    """A restored database (src/server/backups.py) is marked: the next sweep
    empties the account's indexes and puts every clip back."""
    from src.server.search.sync import mark_recreate, run_search_sync_sweep

    lib = _library(env, "Restored")
    clip = _ingest_with(lib, "restored.jpg", _sha(), None)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))
        mark_recreate(s)
        s.commit()
        qw = MagicMock()
        qw.ensure_tenant_index.return_value = False
        qw.ensure_tenant_scene_index.return_value = False
        qw.ensure_tenant_transcript_index.return_value = False
        with patch("src.server.search.sync._get_quickwit", return_value=qw):
            run_search_sync_sweep(s, tenant_id=env[4])
        qw.recreate_tenant_indexes.assert_called_once_with(env[4])
        assert not s.execute(text("SELECT 1 FROM system_metadata WHERE key = 'search.recreate'")).scalar()
        ingested = {d["asset_id"] for call in qw.ingest_tenant_documents.call_args_list for d in call.args[1]}
        assert clip in ingested, "every clip goes back in, synced or not before"
        with patch("src.server.search.sync._get_quickwit", return_value=qw):
            run_search_sync_sweep(s, tenant_id=env[4])
        qw.recreate_tenant_indexes.assert_called_once()  # once: the mark is gone


@pytest.mark.slow
def test_a_restore_mark_waits_while_quickwit_is_down(env):
    from src.server.search.sync import mark_recreate, run_search_sync_sweep

    with _db(env) as s:
        mark_recreate(s)
        s.commit()
        qw = MagicMock()
        qw.recreate_tenant_indexes.side_effect = ConnectionError("Quickwit isn't answering")
        with patch("src.server.search.sync._get_quickwit", return_value=qw):
            run_search_sync_sweep(s, tenant_id=env[4])
        assert s.execute(text("SELECT 1 FROM system_metadata WHERE key = 'search.recreate'")).scalar()
        s.execute(text("DELETE FROM system_metadata WHERE key = 'search.recreate'"))
        s.commit()
