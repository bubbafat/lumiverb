# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What the search sync sweep marks synced: never a write it didn't index.

A description, OCR or correction saved while the sweep runs is newer than the
sweep's start; a write that cleared the clip's sync time (a note, a move)
stays cleared. And a scene described again is stale until search has it.
"""

from __future__ import annotations

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


@pytest.mark.slow
@pytest.mark.parametrize("write", ["description", "note"])
def test_a_write_during_the_sweep_stays_stale(env, write):
    lib = _library(env, f"SweepRace-{write}")
    clip = _ingest_with(lib, f"{write}.jpg", _sha(), None)
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))
        s.execute(text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = :a"), {"a": clip})
        s.commit()

        def meanwhile(other):
            if write == "description":  # a vision save whose inline sync failed
                other.execute(text("INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version,"
                                   " generated_at, data) VALUES (:id, :a, 'm', '1', now(),"
                                   " '{\"description\": \"new words\"}')"), {"id": f"meta_{_sha()[:12]}", "a": clip})
            else:  # a note, as PUT /note saves it: the sync time cleared
                other.execute(text("UPDATE assets SET note = 'new words', search_synced_at = NULL"
                                   " WHERE asset_id = :a"), {"a": clip})

        qw = _sweep_while(env, s, meanwhile)
        sent = [d for d in qw.ingest_tenant_documents.call_args.args[1] if d["asset_id"] == clip]
        assert len(sent) == 1 and "new words" not in (sent[0]["description"], sent[0]["note"])
        assert _stale(s, clip), "the sweep marked synced a write it never indexed"


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
