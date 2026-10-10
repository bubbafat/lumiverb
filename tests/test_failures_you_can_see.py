# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Failures you can see (ADR-016 phase 4, Robert Oct 9).

A clip whose making fails is tried again after 5 minutes, doubling up to a
day, and given up after 10 tries (about two days) until someone asks for
it to be tried again. Settings → Processing and the CLI list failing clips
with their errors, how many tries and when the next one is; Try again
starts the back-off over, given up or not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.server.repository import lineage
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _fail, _library

pytestmark = pytest.mark.slow


def _failures(env, **params) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers/failures", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _due(env, kind: str) -> list[str]:
    from src.server.scheduler.kinds import KINDS
    from src.server.scheduler.queue import candidates

    with _db(env) as s:
        return [i["asset_id"] for i in candidates(s, KINDS[kind], [env[2]])]


def _fail_times(env, clip: str, artifact: str, n: int, error: str = "the model said nothing") -> None:
    with _db(env) as s:
        for _ in range(n):
            lineage.record_failure(s, clip, artifact, error)


def test_a_clip_is_given_up_after_ten_tries(env):
    lib = _library(env, "GiveUp")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    _fail_times(lib, clip, "vision", 9)
    with _db(lib) as s:
        attempts, retry_at = s.execute(text("SELECT attempts, retry_at FROM artifact_lineage"
                                            " WHERE asset_id = :a AND artifact = 'vision'"), {"a": clip}).one()
    assert attempts == 9 and retry_at.year < 9999  # still tried again
    _fail_times(lib, clip, "vision", 1, "the tenth time")
    counts = _counts(lib, "vision")
    assert counts["failing"] == 1 and counts["given_up"] == 1
    [item] = _failures(lib, library_id=lib[2])["items"]
    assert item["given_up"] is True and item["retry_at"] is None and item["attempts"] == 10
    assert item["error"] == "the tenth time"  # the last error
    assert _due(lib, "vision") == []


def test_failing_clips_are_listed_with_why_newest_first(env):
    lib = _library(env, "FailList")
    a = _ingest_with(lib, "Day 1/a.jpg", _sha(), None)
    b = _ingest_with(lib, "Day 1/b.jpg", _sha(), None)
    assert _fail(lib, [{"asset_id": a, "artifact": "ocr", "error": "first"}]).status_code == 200
    assert _fail(lib, [{"asset_id": b, "artifact": "vision", "error": "second"}]).status_code == 200
    items = _failures(lib, library_id=lib[2])["items"]
    assert [(i["asset_id"], i["artifact"]) for i in items] == [(b, "vision"), (a, "ocr")]
    first = items[1]
    assert first["title"] == "Text in images (OCR)" and first["rel_path"] == "Day 1/a.jpg"
    assert first["library_name"] == "FailList" and first["media_type"] == "image"
    assert first["attempts"] == 1 and first["given_up"] is False and first["retry_at"] and first["failed_at"]
    assert [i["asset_id"] for i in _failures(lib, library_id=lib[2], artifact="ocr")["items"]] == [a]


def test_the_list_pages(env):
    lib = _library(env, "FailPages")
    clips = [_ingest_with(lib, f"{i}.jpg", _sha(), None) for i in range(3)]
    for c in clips:
        _fail(lib, [{"asset_id": c, "artifact": "vision", "error": "no"}])
    page = _failures(lib, library_id=lib[2], limit=2)
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = _failures(lib, library_id=lib[2], limit=2, after=page["next_cursor"])
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert {i["asset_id"] for i in page["items"] + rest["items"]} == set(clips)


def test_a_clip_made_at_last_leaves_the_list(env):
    lib = _library(env, "FailMade")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _fail(lib, [{"asset_id": clip, "artifact": "vision", "error": "no"}])
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "a dog",
                                                        "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _failures(lib, library_id=lib[2])["items"] == []


def test_trying_again_starts_the_back_off_over_given_up_or_not(env):
    lib = _library(env, "FailRetry")
    client, headers, *_ = lib
    gave_up = _ingest_with(lib, "a.jpg", _sha(), None)
    waiting = _ingest_with(lib, "b.jpg", _sha(), None)
    _fail_times(lib, gave_up, "vision", 10)
    _fail(lib, [{"asset_id": waiting, "artifact": "vision", "error": "no"}])
    assert _due(lib, "vision") == []
    r = client.post("/v1/producers/failures/retry", json={"asset_ids": [gave_up]}, headers=headers)
    assert r.status_code == 200 and r.json() == {"retried": 1}
    assert _due(lib, "vision") == [gave_up]
    item = [i for i in _failures(lib, library_id=lib[2])["items"] if i["asset_id"] == gave_up][0]
    assert item["attempts"] == 0 and item["given_up"] is False  # the last error stays until it's made
    r = client.post("/v1/producers/failures/retry", json={"artifact": "vision", "library_id": lib[2], "all": True},
                    headers=headers)
    assert r.json() == {"retried": 2}
    assert sorted(_due(lib, "vision")) == sorted([gave_up, waiting])
    with _db(lib) as s:
        assert s.execute(text("SELECT value FROM system_metadata WHERE key = 'scheduler.retry_at'")).scalar()


def test_who_sees_and_who_tries_again(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "FailRoles")
    client, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    _fail(lib, [{"asset_id": clip, "artifact": "vision", "error": "no"}])
    viewer, editor = _key_with_role(env, "viewer"), _key_with_role(env, "editor")
    assert client.get("/v1/producers/failures", params={"library_id": lib[2]}, headers=viewer).status_code == 200
    assert client.post("/v1/producers/failures/retry", json={"asset_ids": [clip]}, headers=viewer).status_code == 403
    assert client.post("/v1/producers/failures/retry", json={"asset_ids": [clip]}, headers=editor).status_code == 200


def test_unknown_producers_are_404(env):
    client, headers, *_ = env
    assert client.get("/v1/producers/failures", params={"artifact": "nope"}, headers=headers).status_code == 404
    assert client.post("/v1/producers/failures/retry", json={"artifact": "nope", "all": True}, headers=headers).status_code == 404


def test_a_trashed_clip_isnt_listed_or_tried_again(env):
    lib = _library(env, "FailTrashed")
    client, headers, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    _fail(lib, [{"asset_id": clip, "artifact": "vision", "error": "no"}])
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET deleted_at = now(), deleted_reason = 'user' WHERE asset_id = :a"),
                  {"a": clip})
        s.commit()
    assert _failures(lib, library_id=lib[2])["items"] == []
    r = client.post("/v1/producers/failures/retry", json={"asset_ids": [clip]}, headers=headers)
    assert r.json() == {"retried": 0}
