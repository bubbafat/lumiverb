# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Pause switches (Robert, Oct 9): one per processing action.

Scans, Upkeep and each producer the scheduler makes each have a switch an
admin pauses and resumes (POST /v1/producers/{target}/pause, /resume). Pause
all (POST /v1/producers/all/pause) pauses every switch and Resume all resumes
every one; nothing stores "all". GET /v1/producers/queue says each switch's
state and the account's, derived: running (green), partly (yellow) or
paused (red). Stopping a producer's redo is something else: what's missing
is still made.
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


def _switches(env, headers=None) -> dict:
    return {s["target"]: s for s in _queue(env, headers)["switches"]}


def test_pause_all_pauses_every_switch_and_resume_all_resumes_every_one(env):
    from src.server.scheduler.service import _on_hold_in_database
    from src.shared.producers import pause_targets

    client, headers, *_ = env
    assert _queue(env)["state"] == "running"
    try:
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        q = _queue(env)
        assert q["state"] == "paused"
        assert [s["target"] for s in q["switches"]] == list(pause_targets())
        assert all(s["paused"] and s["paused_at"] and s["paused_by"] for s in q["switches"])
        assert set(_on_hold_in_database(env[4]).work) == set(pause_targets())
        assert _producers(env)["vision"]["paused"] is True
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204  # again: fine
    finally:
        assert client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers).status_code == 204
    q = _queue(env)
    assert q["state"] == "running" and not any(s["paused"] for s in q["switches"])
    assert set(_on_hold_in_database(env[4]).work) == set()


def test_all_is_named_never_inferred_from_a_missing_target(env):
    # Robert, Oct 9: an impactful action never takes its scope from a missing argument.
    from src.server.scheduler.service import _on_hold_in_database
    from src.shared.producers import PAUSE_ALL, PRODUCERS

    client, headers, *_ = env
    assert PAUSE_ALL == "all" and "all" not in PRODUCERS
    for path in ("/v1/producers/pause", "/v1/producers/resume", "/v1/producers//pause"):
        assert client.post(path, headers=headers).status_code in (404, 405), path
    assert set(_on_hold_in_database(env[4]).work) == set()


def test_one_switch_paused_is_partly_and_the_rest_go_on(env):
    from src.server.scheduler.service import _on_hold_in_database

    client, headers, *_ = env
    try:
        for target in ("vision", "scans", "upkeep"):
            assert client.post(f"/v1/producers/{target}/pause", json={"scope": "work"}, headers=headers).status_code == 204, target
            sw = _switches(env)
            assert sw[target]["paused"] is True and sw[target]["paused_at"] and sw[target]["paused_by"]
            assert _queue(env)["state"] == "partly"
            assert set(_on_hold_in_database(env[4]).work) == {target}
            first = sw[target]["paused_at"]
            assert client.post(f"/v1/producers/{target}/pause", json={"scope": "work"}, headers=headers).status_code == 204  # again
            assert _switches(env)[target]["paused_at"] == first  # who paused it first, and when, stay
            assert client.post(f"/v1/producers/{target}/resume", json={"scope": "work"}, headers=headers).status_code == 204
            assert _queue(env)["state"] == "running" and set(_on_hold_in_database(env[4]).work) == set()
        assert _switches(env)["scans"]["title"] == "Scans" and _switches(env)["upkeep"]["title"] == "Upkeep"
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


def test_every_switch_works_while_all_are_paused_and_the_state_follows(env):
    # Red → unpause one: yellow. Pause all from yellow: red. Unpause the rest one by one: green.
    from src.shared.producers import pause_targets

    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        assert client.post("/v1/producers/scans/resume", json={"scope": "work"}, headers=headers).status_code == 204
        assert _queue(env)["state"] == "partly" and _switches(env)["scans"]["paused"] is False
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        assert _queue(env)["state"] == "paused"
        for target in pause_targets():
            assert client.post(f"/v1/producers/{target}/resume", json={"scope": "work"}, headers=headers).status_code == 204, target
        assert _queue(env)["state"] == "running"
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


def test_pausing_a_producer_and_stopping_its_redo_are_apart(env):
    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/ocr/pause", json={"scope": "work"}, headers=headers).status_code == 204
        assert _producers(env)["ocr"]["redo_stopped"] is False
        assert client.post("/v1/producers/ocr/pause", json={"scope": "redo"}, headers=headers).status_code == 204
        assert client.post("/v1/producers/ocr/resume", json={"scope": "work"}, headers=headers).status_code == 204
        p = _producers(env)["ocr"]
        assert p["paused"] is False and p["redo_stopped"] is True and p["redo_stopped_at"]
    finally:
        client.post("/v1/producers/ocr/resume", json={"scope": "work"}, headers=headers)
        client.post("/v1/producers/ocr/resume", json={"scope": "redo"}, headers=headers)


def test_only_admins_pause_or_resume_and_only_they_see_who(env):
    client, headers, *_ = env
    editor = _key_with_role(env, "editor")
    viewer = _key_with_role(env, "viewer")
    for path in ("/v1/producers/all/pause", "/v1/producers/all/resume", "/v1/producers/ocr/pause",
                 "/v1/producers/ocr/resume", "/v1/producers/scans/pause", "/v1/producers/upkeep/resume"):
        assert client.post(path, headers=editor).status_code == 403, path
        assert client.post(path, headers=viewer).status_code == 403, path
    try:
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        q = _queue(env, viewer)
        assert q["state"] == "paused" and all(s["paused_at"] and s["paused_by"] is None for s in q["switches"])
        p = _producers(env, viewer)["ocr"]
        assert p["paused"] is True and p["paused_by"] is None
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


def test_an_unknown_producer_is_404_and_one_scans_make_is_paused_with_scans(env):
    client, headers, *_ = env
    assert client.post("/v1/producers/nope/pause", json={"scope": "work"}, headers=headers).status_code == 404
    assert client.post("/v1/producers/nope/resume", json={"scope": "work"}, headers=headers).status_code == 404
    r = client.post("/v1/producers/proxy/pause", json={"scope": "work"}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_scheduled", r.text
    assert "Scans" in r.json()["error"]["message"]
    producers = _producers(env)
    assert producers["proxy"]["paused"] is False and producers["proxy"]["scheduled"] is False
    assert producers["vision"]["scheduled"] is True


def test_new_settings_dont_resume_a_paused_producer(env):
    client, headers, *_ = env
    try:
        assert client.post("/v1/producers/analysis_proxy/pause", json={"scope": "work"}, headers=headers).status_code == 204
        r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": 30}, "redo": True},
                       headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["paused"] is True and _producers(env)["analysis_proxy"]["paused"] is True
    finally:
        client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": None}, "redo": True},
                   headers=headers)
        client.post("/v1/producers/analysis_proxy/resume", json={"scope": "work"}, headers=headers)


def test_new_settings_or_a_new_model_lift_no_pause_only_a_toggle_does(env):
    """Robert, Oct 9: "New settings do not lift a pause. Only an explicit toggle does." """
    from src.server.scheduler.service import _on_hold_in_database
    from src.shared.producers import pause_targets
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
            assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
            held = set(pause_targets())
            assert set(_on_hold_in_database(env[4]).work) == held
            r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": 30}, "redo": True},
                           headers=headers)
            assert r.status_code == 200 and r.json()["paused"] is True, r.text
            assert _model(env, "vision", LLAVA, redo=True).status_code == 200
            assert set(_on_hold_in_database(env[4]).work) == held and _queue(env)["state"] == "paused"
    finally:
        assert client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers).status_code == 204  # the toggle
        client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": None}, "redo": True},
                   headers=headers)
        with fake:
            _model(env, "vision", "")
        for m in client.get("/v1/ai", headers=headers).json()["machines"]:
            if not m["built_in"]:
                client.delete(f"/v1/ai/machines/{m['machine_id']}", headers=headers)
    assert set(_on_hold_in_database(env[4]).work) == set()


def test_the_redo_question_says_a_paused_producer_waits(env):
    client, headers, *_ = env
    lib = _library(env, "PausedRedoQuestion")
    sha = _sha()
    _describe(lib, _ingest_with(lib, "a.jpg", sha, None), sha)
    try:
        assert client.post("/v1/producers/vision/pause", json={"scope": "work"}, headers=headers).status_code == 204
        r = client.put("/v1/producers/vision/settings", json={"settings": {"temperature": 0.7}}, headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
        assert "paused" in r.json()["error"]["message"] and r.json()["error"]["details"]["paused"] is True
        assert "all_paused" not in r.json()["error"]["details"]
    finally:
        client.post("/v1/producers/vision/resume", json={"scope": "work"}, headers=headers)


def test_while_upkeep_is_paused_it_deletes_and_changes_nothing_but_search_stays_current(env):
    from unittest.mock import MagicMock, patch

    from tests.test_trash_days import _age, _exists, _ingest, _trash
    from tests.test_trash_days import _sha as _new_sha

    client, headers, *_ = env
    old = _ingest(env, "killswitch/old.mov", sha=_new_sha())
    _trash(env, old)
    _age(env, "assets", "deleted_at", "asset_id", old, 31)
    try:
        assert client.post("/v1/producers/upkeep/pause", json={"scope": "work"}, headers=headers).status_code == 204
        with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()), \
                patch("src.server.repository.tenant.FaceRepository.propagate_assignments") as propagate:
            r = client.post("/v1/upkeep", headers=headers)
            assert r.status_code == 200, r.text
            assert not propagate.called  # names aren't spread to faces meanwhile
            # Skipped for the pause, which reads apart from "nothing to do".
            assert r.json()["face_propagate"]["paused"] is True and r.json()["trash_purge"]["paused"] is True
            assert _exists(env, "assets", "asset_id", old)  # the trash isn't emptied meanwhile
    finally:
        assert client.post("/v1/producers/upkeep/resume", json={"scope": "work"}, headers=headers).status_code == 204
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()), \
            patch("src.server.repository.tenant.FaceRepository.propagate_assignments",
                  return_value={"assigned": 0, "scanned": 0}) as propagate:
        r = client.post("/v1/upkeep", headers=headers)
        assert r.status_code == 200 and propagate.called
        assert r.json()["face_propagate"]["paused"] is False and r.json()["trash_purge"]["paused"] is False
    assert not _exists(env, "assets", "asset_id", old)  # resumed: it goes


def test_pausing_everything_else_leaves_upkeep_running(env):
    from unittest.mock import MagicMock, patch

    from src.shared.producers import pause_targets

    client, headers, *_ = env
    try:
        for target in pause_targets():
            if target != "upkeep":
                assert client.post(f"/v1/producers/{target}/pause", json={"scope": "work"}, headers=headers).status_code == 204
        assert _queue(env)["state"] == "partly"
        with patch("src.server.search.quickwit_client.QuickwitClient", return_value=MagicMock()), \
                patch("src.server.repository.tenant.FaceRepository.propagate_assignments",
                      return_value={"assigned": 0, "scanned": 0}) as propagate:
            r = client.post("/v1/upkeep", headers=headers)
            assert r.status_code == 200 and propagate.called and r.json()["trash_purge"]["paused"] is False
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


def test_while_upkeep_is_paused_no_files_are_cleaned_up_but_a_dry_run_still_reports(env, tmp_path):
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
        assert client.post("/v1/producers/upkeep/pause", json={"scope": "work"}, headers=headers).status_code == 204
        with _db(env) as session:
            skipped = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False)
            assert skipped.orphan_libraries == 0 and skipped.paused is True
            assert stray.exists()
            dry = run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=True)
            assert dry.orphan_libraries == 1 and dry.paused is False
    finally:
        assert client.post("/v1/producers/upkeep/resume", json={"scope": "work"}, headers=headers).status_code == 204
    with _db(env) as session:
        assert run_cleanup_for_tenant(tmp_path, tenant_id, session, dry_run=False).orphan_libraries == 1
    assert not stray.exists()


def test_while_upkeep_is_paused_the_trash_days_question_says_the_purge_waits(env):
    from tests.test_archive_trash_safety import _age, _ingest, _settings, _trash

    client, headers, *_ = env
    clip = _ingest(env, "pause/short.mov", sha=_sha())
    _trash(env, clip)
    _age(env, "assets", "deleted_at", "asset_id", clip, 10)
    try:
        assert client.post("/v1/producers/upkeep/pause", json={"scope": "work"}, headers=headers).status_code == 204
        r = _settings(env, trash_days=7)
        assert r.status_code == 409 and r.json()["error"]["code"] == "trash_days_shortened", r.text
        message = r.json()["error"]["message"]
        assert "within minutes" not in message and "once upkeep is resumed" in message
    finally:
        client.post("/v1/producers/upkeep/resume", json={"scope": "work"}, headers=headers)
    assert "within minutes" in _settings(env, trash_days=7).json()["error"]["message"]


def test_the_queue_gives_paused_work_no_time_but_running_jobs_say_theirs(env):
    """Pausing and time left (Robert, Oct 9): a paused producer has no time and is named;
    with every switch paused no producer has one; a running job still says its time left."""
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
        assert client.post("/v1/producers/vision/pause", json={"scope": "work"}, headers=headers).status_code == 204
        producers_router._work_left_cache.clear()
        eta = _queue(env)["eta"]
        assert eta["producers"]["vision"] is None and eta["producers"]["ocr"] is not None
        assert {"artifact": "vision", "title": _producers(env)["vision"]["title"], "why": "paused"} in eta["not_counted"]
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        eta = _queue(env)["eta"]
        assert all(t is None or t == 0 for t in eta["producers"].values()) and eta["caught_up"] is None
        assert eta["jobs"][0]["left"] is not None  # what's running finishes
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)
