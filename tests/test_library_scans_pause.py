# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Each library's own Scans pause, and who reads it (ADR-018 phase 1).

The account's Scans switch became each library's: an admin pauses and
resumes the libraries it names, or every one (POST /v1/libraries/scans/pause,
/resume; "all" named, never inferred). A paused library's scans find
nothing new, whoever reads it: starting one is a 409 (the scheduler leaves
it out of its pass first: tests/test_scheduler_runners.py). Pause all
pauses every library's scans too, and resuming one library while it's on
leaves everything else paused, each on its own. GET /v1/producers/queue says
each library's reader and pause, and the account's state counts them.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.processing.api import LumiverbAPIError
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_trash_safety import _key_with_role
from tests.test_reconciler import _library

PAUSE, RESUME = "/v1/libraries/scans/pause", "/v1/libraries/scans/resume"
WORK = {"scope": "work"}


def _queue(env, headers=None) -> dict:
    client, own, *_ = env
    r = client.get("/v1/producers/queue", headers=headers or own)
    assert r.status_code == 200, r.text
    return r.json()


def _row(env, library_id: str, headers=None) -> dict:
    [row] = [lib for lib in _queue(env, headers)["libraries"] if lib["library_id"] == library_id]
    return row


def _listed(env, library_id: str) -> dict:
    client, headers, *_ = env
    [lib] = [lib for lib in client.get("/v1/libraries", headers=headers).json() if lib["library_id"] == library_id]
    return lib


@pytest.fixture
def resumed(env):
    """Every library's scans, and every switch, running again after the test."""
    yield
    client, headers, *_ = env
    client.post("/v1/producers/all/resume", json=WORK, headers=headers)


@pytest.mark.slow
def test_a_librarys_scans_pause_on_their_own_and_the_rest_go_on(env, resumed):
    client, headers, *_ = env
    a = _library(env, "PausedAlone")[2]
    b = _library(env, "StillScanned")[2]
    assert _row(env, a) == {"library_id": a, "name": "PausedAlone", "reader": "server", "reader_name": None,
                            "checked_in": None, "paused": False, "paused_at": None, "paused_by": None, "waits": []}
    assert _listed(env, a)["reader"] == "server" and _listed(env, a)["scans_paused"] is False

    assert client.post(PAUSE, json={"library_ids": [a]}, headers=headers).status_code == 204
    row = _row(env, a)
    assert row["paused"] is True and row["paused_at"] and row["paused_by"]
    assert _listed(env, a)["scans_paused"] is True
    assert _row(env, b)["paused"] is False and _listed(env, b)["scans_paused"] is False
    assert _queue(env)["state"] == "partly"
    assert all(not s["paused"] for s in _queue(env)["switches"])  # nothing else is paused

    first = row["paused_at"]
    assert client.post(PAUSE, json={"library_ids": [a]}, headers=headers).status_code == 204  # again: fine
    assert _row(env, a)["paused_at"] == first  # who paused it first, and when, stay

    assert client.post(RESUME, json={"library_ids": [a]}, headers=headers).status_code == 204
    assert _row(env, a)["paused"] is False and _queue(env)["state"] == "running"


@pytest.mark.slow
def test_every_library_is_named_never_inferred(env, resumed):
    # Robert, Oct 9: an impactful action never takes its scope from a missing argument.
    client, headers, *_ = env
    a = _library(env, "EveryOne")[2]
    for body in ({}, {"library_ids": [a], "all": True}):
        r = client.post(PAUSE, json=body, headers=headers)
        assert r.status_code == 400 and r.json()["error"]["code"] == "scope_required", r.text
    assert client.post(PAUSE, json={"library_ids": ["lib_NOPE"]}, headers=headers).status_code == 404
    assert _row(env, a)["paused"] is False

    assert client.post(PAUSE, json={"all": True}, headers=headers).status_code == 204
    q = _queue(env)
    assert q["libraries"] and all(lib["paused"] for lib in q["libraries"])
    assert q["state"] == "partly"  # the switches still run
    assert client.post(RESUME, json={"all": True}, headers=headers).status_code == 204
    assert not any(lib["paused"] for lib in _queue(env)["libraries"])


@pytest.mark.slow
def test_only_admins_pause_a_librarys_scans_and_only_they_see_who(env, resumed):
    client, headers, *_ = env
    a = _library(env, "AdminsOnly")[2]
    editor, viewer = _key_with_role(env, "editor"), _key_with_role(env, "viewer")
    for path in (PAUSE, RESUME):
        for who in (editor, viewer):
            assert client.post(path, json={"library_ids": [a]}, headers=who).status_code == 403, path
    assert client.post(PAUSE, json={"library_ids": [a]}, headers=headers).status_code == 204
    seen = _row(env, a, viewer)
    assert seen["paused"] is True and seen["paused_at"] and seen.get("paused_by") is None


@pytest.mark.slow
def test_pause_all_pauses_every_librarys_scans_and_resuming_one_keeps_the_rest_paused(env, resumed):
    from src.server.scheduler.service import _on_hold_in_database
    from src.shared.producers import pause_targets

    client, headers, *_ = env
    a = _library(env, "UnderPauseAll")[2]
    b = _library(env, "StaysPaused")[2]
    assert client.post("/v1/producers/all/pause", json=WORK, headers=headers).status_code == 204
    q = _queue(env)
    assert q["state"] == "paused" and all(lib["paused"] and lib["paused_by"] for lib in q["libraries"])
    assert _listed(env, a)["scans_paused"] is False  # its own pause isn't set: Pause all is the account's

    assert client.post(RESUME, json={"library_ids": [a]}, headers=headers).status_code == 204
    held = _on_hold_in_database(env[4])
    assert not held.everything
    assert set(held.work) == set(pause_targets())  # every switch, each on its own now
    assert _row(env, a)["paused"] is False and _row(env, b)["paused"] is True
    assert _listed(env, b)["scans_paused"] is True
    assert _queue(env)["state"] == "partly"

    # Resume all: every switch and every library's scans.
    assert client.post("/v1/producers/all/resume", json=WORK, headers=headers).status_code == 204
    q = _queue(env)
    assert q["state"] == "running" and not any(lib["paused"] for lib in q["libraries"])


@pytest.mark.slow
def test_the_old_scans_switch_says_where_it_went(env):
    client, headers, *_ = env
    for action in ("pause", "resume"):
        r = client.post(f"/v1/producers/scans/{action}", json=WORK, headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "per_library", r.text
        assert "/v1/libraries/scans/pause" in r.json()["error"]["message"]
    assert "scans" not in {s["target"] for s in _queue(env)["switches"]}


@pytest.mark.slow
def test_nobody_scans_a_paused_library(env, resumed):
    """Whoever reads it (this server's scheduler, `lumiverb scan`, a device
    later): starting a scan of a paused library is a 409, Pause all's too."""
    client, headers, *_ = env
    a = _library(env, "NotScanned")[2]
    start = {"scope": "", "media_type": "all", "moves": "skip"}

    def started() -> int:
        r = client.post(f"/v1/libraries/{a}/scans", json=start, headers=headers)
        if r.status_code == 200:
            client.delete(f"/v1/scans/{r.json()['scan_id']}", headers=headers)
        else:
            assert r.json()["error"]["code"] == "scans_paused" and r.json()["error"]["details"] == {"library_id": a}
        return r.status_code

    assert started() == 200
    assert client.post(PAUSE, json={"library_ids": [a]}, headers=headers).status_code == 204
    assert started() == 409
    assert client.post(RESUME, json={"library_ids": [a]}, headers=headers).status_code == 204
    assert started() == 200
    assert client.post("/v1/producers/all/pause", json=WORK, headers=headers).status_code == 204
    assert started() == 409


@pytest.mark.fast
def test_a_library_paused_mid_pass_waits_and_isnt_failing(tmp_path):
    """Paused between the scheduler's listing and its scan: the server's 409
    is a pause, not a scan that keeps failing (its changes wait as any
    paused library's do)."""
    from src.server.scheduler.scans import ScanState, scan_pass

    library = {"library_id": "lib_1", "name": "Paused", "root_path": str(tmp_path)}
    state = ScanState()

    def scan_library(*a, **kw):
        raise LumiverbAPIError("scans_paused", "Scans of Paused are paused", 409)

    with (patch("src.processing.roots.reachable_root", lambda library, **kw: tmp_path),
          patch("src.server.scheduler.scans.scan_library", scan_library)):
        scan_pass(MagicMock(), [library], state, now=1000.0)
    assert state.failing == {}

    def failing(*a, **kw):  # any other refusal is still the scan failing
        raise LumiverbAPIError("library_trashed", "The library is in the trash.", 409)

    with (patch("src.processing.roots.reachable_root", lambda library, **kw: tmp_path),
          patch("src.server.scheduler.scans.scan_library", failing)):
        scan_pass(MagicMock(), [library], state, now=1000.0)
    assert list(state.failing) == ["lib_1"]
