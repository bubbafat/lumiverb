# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What the search sync sweep marks synced: never a write it didn't write to search.

A description, OCR or note saved while the sweep runs (after it read the
clip, before it marked it) isn't in the row the sweep wrote, so the clip
stays stale and the next sweep writes it: the sweep marks a clip only if its
row is as it read it (src/server/search/sync.py, xmin). A scene described
again is found by its new words at once: its words are computed with its row.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library

WORD = "quillfeather"  # what the write meanwhile says, and nothing else does


def _sweep(session) -> dict:
    from src.server.search.sync import run_search_sync_sweep

    return run_search_sync_sweep(session)


def _sweep_while(env, session, write_meanwhile) -> list[dict]:
    """Run the sweep; a write commits in its own transaction just as the
    sweep writes the clips' search rows (they were read and built, not yet
    written or marked). Returns the rows the sweep wrote."""
    from src.server.search import sync

    real = sync.put_search_rows
    written: list[dict] = []

    def put(session, rows):
        if rows and not written:
            with _db(env) as other:
                write_meanwhile(other)
                other.commit()
        written.extend(rows)
        real(session, rows)

    with patch.object(sync, "put_search_rows", put):
        _sweep(session)
    return written


def _stale(session, asset_id: str) -> bool:
    from src.server.search.sync import STALE_SEARCH

    return bool(session.execute(
        text(f"SELECT count(*) FROM active_assets a WHERE a.asset_id = :a AND {STALE_SEARCH}"),
        {"a": asset_id}).scalar())


def _row(session, asset_id: str) -> str | None:
    """The clip's search row, its text columns together (None: it has none)."""
    return session.execute(
        text("SELECT concat_ws(' | ', described, note, ocr_text, transcript_text, path_words)"
             " FROM asset_search WHERE asset_id = :a"), {"a": asset_id}).scalar()


def _found(lib, words: str) -> dict[str, dict]:
    """What a search of the library finds: {asset_id: its search context}."""
    client, headers, library_id, *_ = lib
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", f"query:{words}")], headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["search_source"] == "postgres"
    return {i["asset_id"]: i["search_context"] for i in r.json()["items"]}


def _save(other, write: str, clip: str) -> None:
    """A write as the API saves it: a vision or OCR save whose inline sync
    failed (the real repositories, so their own clock and locking), or a
    note (PUT /note clears the sync time)."""
    from src.server.repository.assets import AssetMetadataRepository, AssetOcrRepository

    if write == "description":
        AssetMetadataRepository(other).upsert(clip, "m", "1", {"description": f"a {WORD}", "tags": []})
    elif write == "ocr":
        AssetOcrRepository(other).upsert(clip, WORD.upper(), "m")
    else:
        other.execute(text("UPDATE assets SET note = :n, search_synced_at = NULL WHERE asset_id = :a"),
                      {"n": f"ask about the {WORD}", "a": clip})


def _only_this_clip_stale(session, clip: str) -> None:
    session.execute(text("UPDATE assets SET search_synced_at = now()"))
    session.execute(text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = :a"), {"a": clip})
    session.commit()


def _next_sweep_writes_it(lib, session, clip: str) -> None:
    """What the sweep left stale, the next one writes, and search finds."""
    session.rollback()  # a new snapshot: what's committed now
    _sweep(session)
    assert not _stale(session, clip)
    assert WORD in _row(session, clip).lower()
    assert clip in _found(lib, WORD)


@pytest.mark.slow
@pytest.mark.parametrize("write", ["description", "ocr", "note"])
def test_a_write_during_the_sweep_stays_stale(env, write):
    lib = _library(env, f"SweepRace-{write}")
    clip = _ingest_with(lib, f"{write}.jpg", _sha(), None)
    with _db(env) as s:
        _only_this_clip_stale(s, clip)

        written = _sweep_while(env, s, lambda other: _save(other, write, clip))
        assert [r["asset_id"] for r in written if r["asset_id"] == clip] == [clip], "the sweep wrote the clip once"
        assert WORD not in _row(s, clip).lower(), "the sweep read the clip before the write"
        assert clip not in _found(lib, WORD)
        assert _stale(s, clip), "the sweep marked synced a write it never wrote to search"

        _next_sweep_writes_it(lib, s, clip)


@pytest.mark.slow
@pytest.mark.parametrize("write", ["description", "ocr"])
def test_a_save_timed_before_the_sweep_began_and_committed_after_it_read_stays_stale(env, write):
    """A save takes its time, then commits: a sweep that begins in between
    and reads before the commit is later than the save's time, yet never saw
    it. Its time can't say it's newer; its row can (it's no longer as the
    sweep read it). The save's clock is put a minute back to open the gap,
    whatever the machine's speed or clocks."""
    from src.server.repository import assets

    lib = _library(env, f"SweepGap-{write}")
    clip = _ingest_with(lib, f"gap-{write}.jpg", _sha(), None)
    real_utcnow = assets.utcnow
    with _db(env) as s:
        _only_this_clip_stale(s, clip)

        def save_timed_earlier(other):
            with patch.object(assets, "utcnow", lambda: real_utcnow() - timedelta(minutes=1)):
                _save(other, write, clip)

        written = _sweep_while(env, s, save_timed_earlier)
        assert [r["asset_id"] for r in written if r["asset_id"] == clip] == [clip]
        assert WORD not in _row(s, clip).lower(), "the sweep read before the save"
        assert _stale(s, clip), "the sweep marked synced a save it never wrote to search"

        _next_sweep_writes_it(lib, s, clip)


@pytest.mark.slow
def test_a_scene_described_again_is_found_by_its_new_words_at_once(env):
    """No sync to wait for, and none to miss: a scene's words are its row's."""
    from src.server.repository.video import VideoSceneRepository

    lib = _library(env, "SceneRedo")
    vid = _ingest_with(lib, "w.mov", _sha(), None, media_type="video")
    scene_id = f"scn_{vid}_0"
    with _db(env) as s:
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " description, tags, created_at)"
                       " VALUES (:id, :a, 0, 4000, 9000, 6000, 'a marmot by the river', '[]',"
                       " now() - interval '1 hour')"),
                  {"id": scene_id, "a": vid})
        s.commit()
        assert _found(lib, "marmot")[vid]["hit_type"] == "scene"

        VideoSceneRepository(s).update_vision(scene_id, "m2", "1", "a pelican on a piling", ["bird"])
    hit = _found(lib, "pelican")[vid]
    assert (hit["hit_type"], hit["start_ms"], hit["end_ms"], hit["snippet"]) == ("scene", 4000, 9000,
                                                                                 "a pelican on a piling")
    assert vid in _found(lib, "bird"), "its tags are found too"
    assert vid not in _found(lib, "marmot"), "the old description is still found"


@pytest.mark.slow
def test_everything_again_writes_every_clips_row_synced_or_not(env):
    """POST /v1/upkeep/search-sync?force=true: every clip's row is written
    again, whatever its sync time says (a row lost, say, while marked synced)."""
    client, headers, *_ = env
    lib = _library(env, "EverythingAgain")
    clip = _ingest_with(lib, "zanzibar.jpg", _sha(), None)
    with _db(env) as s:
        _sweep(s)
        assert clip in _found(lib, "zanzibar")
        s.execute(text("DELETE FROM asset_search WHERE asset_id = :a"), {"a": clip})
        s.commit()
        assert not _stale(s, clip) and clip not in _found(lib, "zanzibar")

    r = client.post("/v1/upkeep/search-sync", params={"force": "true"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["synced"] >= 1 and r.json()["failed"] == 0
    assert clip in _found(lib, "zanzibar")


@pytest.mark.slow
def test_a_batch_that_fails_leaves_its_clips_for_the_next_sweep(env):
    from src.server.search import sync

    lib = _library(env, "SweepFails")
    clip = _ingest_with(lib, "kestrel.jpg", _sha(), None)
    with _db(env) as s:
        _only_this_clip_stale(s, clip)

        def broken(session, rows):
            raise RuntimeError("the write failed")

        with patch.object(sync, "put_search_rows", broken):
            result = _sweep(s)
        assert result["failed"] == 1 and result["synced"] == 0
        assert _stale(s, clip) and _row(s, clip) is None, "nothing marked that wasn't written"

        assert _sweep(s)["synced"] >= 1
        assert not _stale(s, clip) and "kestrel" in _row(s, clip)
    assert clip in _found(lib, "kestrel")
