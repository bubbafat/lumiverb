# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Pausing processing (Robert, Oct 9): all of it, or one producer's.

An admin pauses all of the account's processing (POST /v1/producers/pause)
or one producer's (POST /v1/producers/{artifact}/pause), and resumes it:
the scheduler starts nothing more of it, and what's running finishes.
GET /v1/producers/queue says whether all of it is paused; GET
/v1/producers says it of each producer, beside whether its redo is stopped
(which is something else: what's missing is still made).
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _sha
from tests.test_archive_trash_safety import _key_with_role
from tests.test_lineage_api import _ingest_with
from tests.test_reconciler import _describe, _library

pytestmark = pytest.mark.slow


def _producers(env, headers=None) -> dict:
    client, own, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers or own)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}


def _queue(env, headers=None) -> dict:
    client, own, *_ = env
    r = client.get("/v1/producers/queue", headers=headers or own)
    assert r.status_code == 200, r.text
    return r.json()


def test_an_admin_pauses_all_processing_and_resumes_it(env):
    from src.server.scheduler.service import _on_hold_in_database

    client, headers, *_ = env
    assert _queue(env)["paused"] is False
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        q = _queue(env)
        assert q["paused"] is True and q["paused_at"] and q["paused_by"]
        assert "all" in _on_hold_in_database(env[4])
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204  # again: fine
        assert _queue(env)["paused_at"] == q["paused_at"]  # who paused it first, and when, stay
        assert not any(p["paused"] for p in _producers(env).values())  # each producer's own is apart
    finally:
        assert client.post("/v1/producers/resume", headers=headers).status_code == 204
    q = _queue(env)
    assert q["paused"] is False and q["paused_at"] is None and q["paused_by"] is None
    assert "all" not in _on_hold_in_database(env[4])
    assert client.post("/v1/producers/resume", headers=headers).status_code == 204  # not paused: fine


def test_an_admin_pauses_one_producer_and_resumes_it(env):
    from src.server.scheduler.service import _on_hold_in_database

    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/vision/pause", headers=headers).status_code == 204
        producers = _producers(env)
        assert producers["vision"]["paused"] is True and producers["vision"]["paused_at"]
        assert producers["vision"]["paused_by"]
        assert not producers["ocr"]["paused"] and _queue(env)["paused"] is False
        assert _on_hold_in_database(env[4]) == {"vision"}
    finally:
        assert client.post("/v1/producers/vision/resume", headers=headers).status_code == 204
    assert _producers(env)["vision"]["paused"] is False and _producers(env)["vision"]["paused_at"] is None
    assert _on_hold_in_database(env[4]) == set()


def test_pausing_a_producer_and_stopping_its_redo_are_apart(env):
    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/ocr/pause", headers=headers).status_code == 204
        assert _producers(env)["ocr"]["redo_stopped"] is False
        assert client.post("/v1/producers/ocr/redo/stop", headers=headers).status_code == 204
        assert client.post("/v1/producers/ocr/resume", headers=headers).status_code == 204
        p = _producers(env)["ocr"]
        assert p["paused"] is False and p["redo_stopped"] is True and p["redo_stopped_at"]
    finally:
        client.post("/v1/producers/ocr/resume", headers=headers)
        client.post("/v1/producers/ocr/redo/resume", headers=headers)


def test_only_admins_pause_or_resume_and_only_they_see_who(env):
    client, headers, *_ = env
    editor = _key_with_role(env, "editor")
    viewer = _key_with_role(env, "viewer")
    for path in ("/v1/producers/pause", "/v1/producers/resume", "/v1/producers/ocr/pause",
                 "/v1/producers/ocr/resume"):
        assert client.post(path, headers=editor).status_code == 403, path
        assert client.post(path, headers=viewer).status_code == 403, path
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        assert client.post("/v1/producers/ocr/pause", headers=headers).status_code == 204
        q = _queue(env, viewer)
        assert q["paused"] is True and q["paused_at"] and q["paused_by"] is None
        p = _producers(env, viewer)["ocr"]
        assert p["paused"] is True and p["paused_by"] is None
    finally:
        client.post("/v1/producers/resume", headers=headers)
        client.post("/v1/producers/ocr/resume", headers=headers)


def test_an_unknown_producer_is_404_and_one_scans_make_is_paused_with_everything(env):
    client, headers, *_ = env
    assert client.post("/v1/producers/nope/pause", headers=headers).status_code == 404
    assert client.post("/v1/producers/nope/resume", headers=headers).status_code == 404
    r = client.post("/v1/producers/proxy/pause", headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_scheduled", r.text
    assert "pausing all processing" in r.json()["error"]["message"]
    producers = _producers(env)
    assert producers["proxy"]["paused"] is False and producers["proxy"]["scheduled"] is False
    assert producers["vision"]["scheduled"] is True


def test_new_settings_dont_resume_a_paused_producer(env):
    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/analysis_proxy/pause", headers=headers).status_code == 204
        r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": 30}, "redo": True},
                       headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["paused"] is True and _producers(env)["analysis_proxy"]["paused"] is True
    finally:
        client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": None}, "redo": True},
                   headers=headers)
        client.post("/v1/producers/analysis_proxy/resume", headers=headers)


def test_the_redo_question_says_a_paused_producer_waits(env):
    client, headers, *_ = env
    lib = _library(env, "PausedRedoQuestion")
    sha = _sha()
    _describe(lib, _ingest_with(lib, "a.jpg", sha, None), sha)
    try:
        assert client.post("/v1/producers/vision/pause", headers=headers).status_code == 204
        r = client.put("/v1/producers/vision/settings", json={"settings": {"temperature": 0.7}}, headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
        assert "paused" in r.json()["error"]["message"] and r.json()["error"]["details"]["paused"] is True
    finally:
        client.post("/v1/producers/vision/resume", headers=headers)
