# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Asking the scheduler for work now, and naming what's acted on (ADR-016 phase 4).

POST /v1/producers/run is what `lumiverb enrich` asks: a producer's (or
every producer's) failing clips tried again at once, in a library or all
of them, and with scope redo what's made made again. Retrying failures
names what it retries too. "all" is named, never inferred from a missing
target (Robert, Oct 9): a request without a target is a 400.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.server.repository import lineage
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_trash_safety import _key_with_role
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _library

pytestmark = pytest.mark.slow


def _run(env, headers=None, **body):
    client, own, *_ = env
    return client.post("/v1/producers/run", json=body, headers=headers or own)


def _code(r) -> str:
    return r.json()["error"]["code"]


def _described(lib, rel_path: str) -> str:
    """A clip whose description a current producer made."""
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, rel_path, sha, None)
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "a dog",
                                                        "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    return clip


def test_run_names_its_producer_and_libraries(env):
    lib = _library(env, "RunTargets")
    for body in ({"producer": "vision", "scope": "new"}, {"producer": "vision", "scope": "new", "all": True,
                                                          "library_ids": [lib[2]]}):
        r = _run(lib, **body)
        assert r.status_code == 400 and _code(r) == "scope_required", body
    assert _run(lib, library_ids=[lib[2]], scope="new").status_code == 422  # the producer is named (or all)
    assert _run(lib, producer="vision", library_ids=[lib[2]]).status_code == 422  # and new work or a redo
    assert _run(lib, producer="nope", library_ids=[lib[2]], scope="new").status_code == 404
    assert _run(lib, producer="vision", library_ids=["lib_nope"], scope="new").status_code == 404
    r = _run(lib, producer="proxy", library_ids=[lib[2]], scope="new")
    assert r.status_code == 409 and _code(r) == "not_scheduled"


def test_run_tries_failing_clips_again_now_and_says_whats_left(env):
    lib = _library(env, "RunNew")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    with _db(lib) as s:
        for _ in range(10):  # given up
            lineage.record_failure(s, clip, "vision", "no")
    r = _run(lib, producer="vision", library_ids=[lib[2]], scope="new")
    assert r.status_code == 200, r.text
    [p] = r.json()["producers"]
    assert (p["artifact"], p["retried"], p["missing"]) == ("vision", 1, 1)
    assert _counts(lib, "vision")["given_up"] == 0
    with _db(lib) as s:  # the scheduler is asked to look again now
        assert s.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.retry_at'")).scalar()
    every = _run(lib, producer="all", all=True, scope="new").json()["producers"]
    from src.shared.producers import PRODUCERS

    assert [p["artifact"] for p in every] == [a for a, p in PRODUCERS.items() if p.scheduled]


def test_a_redo_makes_whats_made_again_in_the_library_named_and_keeps_a_persons(env):
    lib = _library(env, "RunRedo")
    other = _library(env, "RunRedoOther")
    clip = _described(lib, "a.jpg")
    kept = _described(other, "b.jpg")
    assert _counts(lib, "vision")["stale"] == 0
    r = _run(lib, producer="vision", library_ids=[lib[2]], scope="redo")
    assert r.status_code == 200, r.text
    [p] = r.json()["producers"]
    assert p["redo"] == 1 and _counts(lib, "vision")["stale"] == 1
    assert _counts(other, "vision")["stale"] == 0  # another library: untouched
    from src.server.scheduler.kinds import KINDS
    from src.server.scheduler.queue import candidates

    with _db(lib) as s:
        want = lineage.desired(s, "vision")
        assert [i["asset_id"] for i in candidates(s, KINDS["redo_vision"], [lib[2]], want=want)] == [clip]
        # A person's is never made again.
        s.execute(text("UPDATE artifact_lineage SET producer = 'person', settings_hash = '' WHERE asset_id = :a"
                       " AND artifact = 'vision'"), {"a": kept})
        s.commit()
    _run(other, producer="vision", library_ids=[other[2]], scope="redo")
    with _db(other) as s:
        row = s.execute(text("SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = 'vision'"),
                        {"a": kept}).scalar()
    assert row == "person"


def test_a_redo_is_an_admins_and_says_when_its_redo_is_stopped(env):
    lib = _library(env, "RunRedoWho")
    editor = _key_with_role(lib, "editor")
    assert _run(lib, editor, producer="vision", library_ids=[lib[2]], scope="redo").status_code == 403
    assert _run(lib, editor, producer="vision", library_ids=[lib[2]], scope="new").status_code == 200
    client, headers, *_ = lib
    try:
        assert client.post("/v1/producers/vision/pause", json={"scope": "redo"}, headers=headers).status_code == 204
        [p] = _run(lib, producer="vision", library_ids=[lib[2]], scope="redo").json()["producers"]
        assert p["redo_stopped"] is True and p["paused"] is False
    finally:
        client.post("/v1/producers/vision/resume", json={"scope": "redo"}, headers=headers)
    r = _run(lib, producer="proxy", library_ids=[lib[2]], scope="redo")  # made by scans: not the scheduler's
    assert r.status_code == 409 and _code(r) == "not_scheduled"


def test_retrying_failures_names_what_it_retries(env):
    lib = _library(env, "RetryNamed")
    client, headers, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    with _db(lib) as s:
        lineage.record_failure(s, clip, "vision", "no")
    for body in ({}, {"library_id": lib[2]}, {"artifact": "vision"}, {"asset_ids": [clip], "all": True}):
        r = client.post("/v1/producers/failures/retry", json=body, headers=headers)
        assert r.status_code == 400 and _code(r) == "scope_required", body
    assert _counts(lib, "vision")["failing"] == 1
    r = client.post("/v1/producers/failures/retry", json={"all": True}, headers=headers)
    assert r.status_code == 200 and r.json()["retried"] >= 1


def test_a_pause_names_its_scope(env):
    client, headers, *_ = env
    assert client.post("/v1/producers/vision/pause", headers=headers).status_code == 422
    r = client.post("/v1/producers/scans/pause", json={"scope": "redo"}, headers=headers)
    assert r.status_code == 422 and _code(r) == "bad_scope"
