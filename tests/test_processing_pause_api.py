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
        # The queue names it too, whatever scope the counts are for (the page's header reads it there).
        assert _queue(env)["paused_producers"] == [{"artifact": "vision", "title": producers["vision"]["title"]}]
    finally:
        assert client.post("/v1/producers/vision/resume", headers=headers).status_code == 204
    assert _producers(env)["vision"]["paused"] is False and _producers(env)["vision"]["paused_at"] is None
    assert _on_hold_in_database(env[4]) == set()
    assert _queue(env)["paused_producers"] == []


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
        assert client.post("/v1/producers/ocr/pause", headers=headers).status_code == 204
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
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
    assert "pausing scans" in r.json()["error"]["message"]
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


def test_new_settings_or_a_new_model_lift_no_pause_only_a_toggle_does(env):
    """Robert, Oct 9: "New settings do not lift a pause. Only an explicit toggle does."
    Not a producer's own pause, not scans', not all of it."""
    from src.server.scheduler.service import _on_hold_in_database
    from tests.test_ai_machines import BRAIN, LLAVA, QWEN, _add, _machines, _model

    client, headers, *_ = env
    for m in client.get("/v1/ai", headers=headers).json()["machines"]:
        if not m["built_in"]:
            client.delete(f"/v1/ai/machines/{m['machine_id']}", headers=headers)
    fake, _ = _machines({BRAIN: (QWEN, LLAVA)})
    try:
        with fake:
            assert _add(env).status_code == 201
            assert _model(env, "vision", QWEN, redo=True).status_code == 200
            for path in ("/v1/producers/analysis_proxy/pause", "/v1/producers/vision/pause",
                         "/v1/producers/scans/pause", "/v1/producers/pause"):
                assert client.post(path, headers=headers).status_code == 204, path
            held = {"all", "scans", "analysis_proxy", "vision"}
            assert _on_hold_in_database(env[4]) == held

            r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": 30}, "redo": True},
                           headers=headers)
            assert r.status_code == 200 and r.json()["paused"] is True, r.text
            assert _model(env, "vision", LLAVA, redo=True).status_code == 200
            assert _on_hold_in_database(env[4]) == held
            q = _queue(env)
            assert q["paused"] is True and q["scans_paused"] is True
            assert _producers(env)["vision"]["paused"] is True
    finally:
        assert client.post("/v1/producers/resume", headers=headers).status_code == 204  # the toggle
        client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": None}, "redo": True},
                   headers=headers)
        with fake:
            _model(env, "vision", "")
        for m in client.get("/v1/ai", headers=headers).json()["machines"]:
            if not m["built_in"]:
                client.delete(f"/v1/ai/machines/{m['machine_id']}", headers=headers)
    assert _on_hold_in_database(env[4]) == set()


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
        assert r.json()["error"]["details"]["all_paused"] is False
        assert client.post("/v1/producers/vision/resume", headers=headers).status_code == 204
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        r = client.put("/v1/producers/vision/settings", json={"settings": {"temperature": 0.7}}, headers=headers)
        assert r.status_code == 409, r.text
        assert "All processing is paused" in r.json()["error"]["message"]
        assert r.json()["error"]["details"]["all_paused"] is True and r.json()["error"]["details"]["paused"] is False
    finally:
        client.post("/v1/producers/vision/resume", headers=headers)
        client.post("/v1/producers/resume", headers=headers)


def test_an_admin_pauses_scans_alone_and_resumes_them(env):
    from src.server.scheduler.service import _on_hold_in_database

    client, headers, *_ = env
    viewer = _key_with_role(env, "viewer")
    editor = _key_with_role(env, "editor")
    assert _queue(env)["scans_paused"] is False
    for path in ("/v1/producers/scans/pause", "/v1/producers/scans/resume"):
        assert client.post(path, headers=editor).status_code == 403, path
    try:
        assert client.post("/v1/producers/scans/pause", headers=headers).status_code == 204
        q = _queue(env)
        assert q["scans_paused"] is True and q["scans_paused_at"] and q["scans_paused_by"]
        assert q["paused"] is False  # the rest goes on
        assert _on_hold_in_database(env[4]) == {"scans"}
        assert client.post("/v1/producers/scans/pause", headers=headers).status_code == 204  # again: fine
        assert _queue(env)["scans_paused_at"] == q["scans_paused_at"]
        seen = _queue(env, viewer)
        assert seen["scans_paused"] is True and seen["scans_paused_by"] is None
    finally:
        assert client.post("/v1/producers/scans/resume", headers=headers).status_code == 204
        client.post("/v1/producers/resume", headers=headers)
    q = _queue(env)
    assert q["scans_paused"] is False and q["scans_paused_at"] is None
    assert _on_hold_in_database(env[4]) == set()


# ---------------------------------------------------------------------------
# Pause all is the kill switch (Robert, Oct 9): everything but the website
# stops; Resume all turns everything back on, keeping no paused part.
# ---------------------------------------------------------------------------


def test_resume_all_turns_everything_back_on_scans_and_producers_included(env):
    from src.server.scheduler.service import _on_hold_in_database

    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/scans/pause", headers=headers).status_code == 204
        assert client.post("/v1/producers/ocr/pause", headers=headers).status_code == 204
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        assert client.post("/v1/producers/resume", headers=headers).status_code == 204
        assert _on_hold_in_database(env[4]) == set()
        q = _queue(env)
        assert q["paused"] is False and q["scans_paused"] is False
        assert _producers(env)["ocr"]["paused"] is False
    finally:
        client.post("/v1/producers/resume", headers=headers)


def test_while_everything_is_paused_its_parts_cant_be_switched_alone(env):
    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        for path in ("/v1/producers/scans/pause", "/v1/producers/scans/resume", "/v1/producers/ocr/pause",
                     "/v1/producers/ocr/resume"):
            r = client.post(path, headers=headers)
            assert r.status_code == 409 and r.json()["error"]["code"] == "all_paused", (path, r.text)
            assert "Resume all" in r.json()["error"]["message"]
    finally:
        client.post("/v1/producers/resume", headers=headers)


def test_while_everything_is_paused_upkeep_deletes_and_changes_nothing_but_search_stays_current(env):
    from unittest.mock import MagicMock, patch

    from tests.test_trash_days import _age, _exists, _ingest, _trash
    from tests.test_trash_days import _sha as _new_sha

    client, headers, *_ = env
    old = _ingest(env, "killswitch/old.mov", sha=_new_sha())
    _trash(env, old)
    _age(env, "assets", "deleted_at", "asset_id", old, 31)
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()), \
                patch("src.server.repository.tenant.FaceRepository.propagate_assignments") as propagate:
            r = client.post("/v1/upkeep", headers=headers)
            assert r.status_code == 200, r.text
            assert not propagate.called  # names aren't spread to faces meanwhile
            # Skipped for the pause, which reads apart from "nothing to do".
            assert r.json()["face_propagate"]["paused"] is True and r.json()["trash_purge"]["paused"] is True
            assert _exists(env, "assets", "asset_id", old)  # the trash isn't emptied meanwhile
    finally:
        assert client.post("/v1/producers/resume", headers=headers).status_code == 204
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()), \
            patch("src.server.repository.tenant.FaceRepository.propagate_assignments",
                  return_value={"assigned": 0, "scanned": 0}) as propagate:
        r = client.post("/v1/upkeep", headers=headers)
        assert r.status_code == 200 and propagate.called
        assert r.json()["face_propagate"]["paused"] is False and r.json()["trash_purge"]["paused"] is False
    assert not _exists(env, "assets", "asset_id", old)  # resumed: it goes


def test_while_everything_is_paused_no_files_are_cleaned_up_but_a_dry_run_still_reports(env, tmp_path):
    from unittest.mock import MagicMock, patch

    from src.server.search.cleanup import run_cleanup_single_tenant
    from tests.test_lineage_api import _db

    def run_cleanup_for_tenant(data_dir, tenant_id, session, *, dry_run):
        with patch("src.server.config.get_settings", return_value=MagicMock(data_dir=str(data_dir))):
            return run_cleanup_single_tenant(tenant_id, session, dry_run=dry_run)

    client, headers, *_ = env
    tenant_id = env[4]
    stray = tmp_path / tenant_id / "lib_gone" / "x.jpg"  # a library no longer in the database
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        with _db(env) as session:
            skipped = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)
            assert skipped.orphan_libraries == 0 and skipped.paused is True
            assert stray.exists()
            dry = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=True)
            assert dry.orphan_libraries == 1 and dry.paused is False
    finally:
        assert client.post("/v1/producers/resume", headers=headers).status_code == 204
    with _db(env) as session:
        assert run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False).orphan_libraries == 1
    assert not stray.exists()


def test_while_everything_is_paused_the_trash_days_question_says_the_purge_waits(env):
    from tests.test_archive_trash_safety import _age, _ingest, _settings, _trash

    client, headers, *_ = env
    clip = _ingest(env, "pause/short.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 10)
    try:
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        r = _settings(env, trash_days=7)
        assert r.status_code == 409 and r.json()["error"]["code"] == "trash_days_shortened", r.text
        message = r.json()["error"]["message"]
        assert "within minutes" not in message and "once processing is resumed" in message
    finally:
        client.post("/v1/producers/resume", headers=headers)
    assert "within minutes" in _settings(env, trash_days=7).json()["error"]["message"]


def test_the_queue_gives_paused_work_no_time_but_running_jobs_say_theirs(env):
    """Pausing and time left (Robert, Oct 9): a paused producer has no time and is named;
    under Pause all no producer has one; a running job still says its time left, since it finishes."""
    from src.server.api.routers import producers as producers_router
    from src.shared.utils import utcnow
    from tests.test_lineage_api import _ingest_with
    from tests.test_scheduler_status_api import _write

    client, headers, *_ = env
    _ingest_with(_library(env, "PausedEta"), "a.jpg", _sha(), None)  # a photo nothing has described yet
    _write(env, {"at": utcnow().isoformat(), "running": {"vision": 1}, "waiting": {},
                 "pools": {"vision": [1, 3], "gpu": [0, 1], "probe": [0, 1]},
                 "pace": {"vision": 6.0, "ocr": 3.0, "clip": 0.5, "faces": 0.5},
                 "jobs": [{"kind": "vision", "units": 1.0, "elapsed": 2.0}]})
    try:
        assert client.post("/v1/producers/vision/pause", headers=headers).status_code == 204
        producers_router._work_left_cache.clear()
        eta = _queue(env)["eta"]
        assert eta["producers"]["vision"] is None and eta["producers"]["ocr"] is not None
        assert {"artifact": "vision", "title": _producers(env)["vision"]["title"], "why": "paused"} in eta["not_counted"]
        assert client.post("/v1/producers/pause", headers=headers).status_code == 204
        eta = _queue(env)["eta"]
        assert all(t is None or t == 0 for t in eta["producers"].values()) and eta["caught_up"] is None
        assert eta["jobs"][0]["left"] is not None  # what's running finishes
    finally:
        client.post("/v1/producers/resume", headers=headers)
