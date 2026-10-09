# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What the scheduler finds due, straight from the database (ADR-016 phase 4).

Each kind lists its due clips (the reconciler's: missing, or made from a
file that has since changed, less failures waiting their turn) oldest
first, across the libraries asked about, with what its job needs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.server.scheduler.kinds import KINDS
from src.server.scheduler.queue import candidates
from tests.test_analysis_proxy_api import _ingest, _upload, env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


def _due(env, kind: str, libraries: list[str] | None = None) -> list[str]:
    with _db(env) as s:
        return [r["asset_id"] for r in candidates(s, KINDS[kind], [env[2]] if libraries is None else libraries)]


def test_a_kind_lists_its_due_clips_oldest_first(env) -> None:
    lib = _library(env, "Oldest first")
    first = _ingest(lib, "a.mov")
    second = _ingest(lib, "b.mov")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET created_at = now() - interval '1 day' WHERE asset_id = :a"), {"a": second})
        s.commit()
    assert _due(lib, "probe", [lib[2]]) == [second, first]


def test_only_the_libraries_asked_about(env) -> None:
    here, there = _library(env, "Asked"), _library(env, "Not asked")
    mine = _ingest(here, "mine.mov")
    _ingest(there, "theirs.mov")
    assert _due(here, "probe", [here[2]]) == [mine]
    assert _due(here, "probe", []) == []


def test_a_kind_lists_only_its_media(env) -> None:
    lib = _library(env, "Media")
    video = _ingest(lib, "v.mov")
    photo = _ingest_with(lib, "p.jpg", _sha(), None)
    assert _due(lib, "probe", [lib[2]]) == [video]
    assert _due(lib, "vision", [lib[2]]) == [photo]


def test_a_clip_made_now_is_no_longer_due(env) -> None:
    lib = _library(env, "Made")
    client, headers, *_ = lib
    sha = _sha()
    photo = _ingest_with(lib, "p.jpg", sha, None)
    r = client.post(f"/v1/assets/{photo}/vision", json={"model_id": "m", "description": "a dog",
                                                         "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert photo not in _due(lib, "vision", [lib[2]])


def test_a_failure_waiting_its_turn_is_not_due(env) -> None:
    lib = _library(env, "Failed")
    client, headers, *_ = lib
    photo = _ingest_with(lib, "p.jpg", _sha(), None)
    r = client.post("/v1/producers/failures", json={"items": [
        {"asset_id": photo, "artifact": "vision", "error": "the model said nothing"}]}, headers=headers)
    assert r.status_code < 300, r.text
    assert photo not in _due(lib, "vision", [lib[2]])
    assert photo in _due(lib, "ocr", [lib[2]])  # its other work isn't held up


def test_transcripts_wait_for_the_analysis_copy(env) -> None:
    lib = _library(env, "Transcripts")
    video = _ingest(lib, "talk.mov")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": video})
        s.commit()
    assert video not in _due(lib, "transcript", [lib[2]])
    _upload(lib, video)
    assert video in _due(lib, "transcript", [lib[2]])


def test_an_item_says_what_its_job_needs(env) -> None:
    lib = _library(env, "Fields")
    sha = _sha()
    video = _ingest(lib, "Day 1/clip.mov", sha=sha)
    with _db(env) as s:
        [item] = candidates(s, KINDS["probe"], [lib[2]])
    assert item["asset_id"] == video
    assert item["library_id"] == lib[2]
    assert item["rel_path"] == "Day 1/clip.mov"
    assert item["sha256"] == sha
    assert item["has_analysis_proxy"] is False
    assert item["created_at"][:2] == "20"


def test_scans_are_not_in_the_database(env) -> None:
    assert _due(env, "scan") == []


def test_clips_in_hand_or_just_tried_are_left_out_so_the_rest_are_reached(env):
    # Otherwise the oldest ones, held back, would fill every answer and the
    # clips behind them would never be listed (review, Oct 9).
    lib = _library(env, "Skip")
    first = _ingest(lib, "a.mov")
    second = _ingest(lib, "b.mov")
    with _db(env) as s:
        assert [r["asset_id"] for r in candidates(s, KINDS["probe"], [lib[2]], limit=1)] == [first]
        assert [r["asset_id"] for r in candidates(s, KINDS["probe"], [lib[2]], limit=1, skip=[first])] == [second]
