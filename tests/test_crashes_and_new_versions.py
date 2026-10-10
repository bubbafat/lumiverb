# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Crashes charged after so many in a row; a new producer version's redo
waits for an admin (review, Oct 9).

A job that crashes charges no clip, but each clip's crashes in a row are
kept (producer_crashes): the CRASHES_BEFORE_CHARGE-th is charged, so the
back-off and giving up apply. A success or a charged failure starts it over.

A producer's new version is like new settings nobody approved: the first
time the scheduler sees clips an older version made, the producer's redo is
stopped; Processing shows it stopped with the stale count, and an admin
resumes it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.server.repository import lineage
from src.server.scheduler.kinds import KINDS
from src.server.scheduler.service import _crashed_in_database, _from_database
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


def _crashes(env, clip: str, artifact: str) -> int | None:
    with _db(env) as s:
        return s.execute(text("SELECT crashes FROM producer_crashes WHERE asset_id = :a AND artifact = :b"),
                         {"a": clip, "b": artifact}).scalar()


def test_a_clip_is_charged_after_so_many_crashes_in_a_row(env):
    lib = _library(env, "Crashes")
    clip = _ingest_with(lib, "a.jpg", _sha())
    tenant_id = env[4]
    for n in range(1, lineage.CRASHES_BEFORE_CHARGE):
        assert _crashed_in_database(tenant_id, "ocr", [clip, "ast_gone"], "boom") == []
        assert _crashes(lib, clip, "ocr") == n
    assert _crashed_in_database(tenant_id, "ocr", [clip], "boom") == [clip]
    assert _crashes(lib, clip, "ocr") is None  # starts over
    assert _crashes(lib, "ast_gone", "ocr") is None  # a clip that's gone isn't kept


def test_a_success_or_a_charged_failure_starts_the_count_over(env):
    lib = _library(env, "CrashesReset")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    tenant_id = env[4]
    _crashed_in_database(tenant_id, "vision", [clip], "boom")
    _crashed_in_database(tenant_id, "ocr", [clip], "boom")
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "a dog",
                                                        "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _crashes(lib, clip, "vision") is None
    r = client.post("/v1/producers/failures", json={"items": [{"asset_id": clip, "artifact": "ocr", "error": "x"}]},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _crashes(lib, clip, "ocr") is None


def _made_by_an_older_version(lib, name: str) -> str:
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, name, sha)
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "old words",
                                                        "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(lib) as s:  # as if vision's version went up since
        s.execute(text("UPDATE artifact_lineage SET producer_version = '0' WHERE asset_id = :a AND artifact = 'vision'"),
                  {"a": clip})
        s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.vision.version_seen'"))
        s.commit()
    return clip


def _set_model(env, job: str, model: str) -> None:
    from src.server.database import get_control_session
    from src.server.models.control_plane import Tenant
    from src.server.repository.ai_machines import set_job_model

    with get_control_session() as ctrl:
        tenant = ctrl.get(Tenant, env[4])
        set_job_model(tenant, job, model)
        ctrl.add(tenant)
        ctrl.commit()


def test_a_new_producer_version_waits_for_an_admin_to_resume_its_redo(env):
    """Review (Oct 9): a version bump redid the whole library with nobody asked."""
    from tests.test_redo_on_change import VISION

    _set_model(env, "vision", VISION)
    lib = _library(env, "NewVersion")
    client, headers, library_id, *_ = lib
    clip = _made_by_an_older_version(lib, "a.jpg")
    redo = KINDS["redo_vision"]
    try:
        assert _from_database(env[4], redo, [library_id]) is None  # stopped, not handed out
        r = client.get("/v1/producers", headers=headers)
        p = {p["artifact"]: p for p in r.json()["producers"]}["vision"]
        assert p["redo_stopped"] is True and p["counts"]["stale"] >= 1  # what Processing shows
        assert _from_database(env[4], redo, [library_id]) is None  # still stopped
        assert client.post("/v1/producers/vision/resume", json={"scope": "redo"}, headers=headers).status_code == 204
        assert clip in [i["asset_id"] for i in _from_database(env[4], redo, [library_id])]  # resuming sticks
    finally:
        client.post("/v1/producers/vision/resume", json={"scope": "redo"}, headers=headers)


def test_without_clips_an_older_version_made_nothing_is_stopped(env):
    lib = _library(env, "SameVersion")
    with _db(lib) as s:
        s.execute(text("UPDATE artifact_lineage SET producer_version = :v WHERE artifact = 'vision'"),
                  {"v": lineage.PRODUCERS["vision"].version})
        s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.vision.version_seen'"))
        s.commit()
        assert lineage.hold_new_version(s, "vision") is False
        assert "vision" not in lineage.pauses(s).redo


def test_the_database_away_is_a_503_the_scheduler_charges_no_clip_for():
    """A lost connection or a deadlock is the request's trouble for now:
    503 (runners.whose: transient), not a 500 (counted as a crash)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import InterfaceError, OperationalError

    from src.server.api import main
    from src.producers.runner import NOT_THE_CLIPS

    assert main.app.exception_handlers[OperationalError] is main._database_unavailable
    assert main.app.exception_handlers[InterfaceError] is main._database_unavailable
    app = FastAPI()
    app.add_exception_handler(OperationalError, main._database_unavailable)

    @app.get("/away")
    def away():
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))

    r = TestClient(app).get("/away")
    assert r.status_code == 503 and r.status_code in NOT_THE_CLIPS
    assert r.json()["error"]["code"] == "database_unavailable"


def test_a_database_error_that_recurs_is_a_500_counted_as_a_crash():
    """A value too long for an index, a statement timeout, a full disk: they
    come back every try, so they aren't 'unavailable'."""
    from types import SimpleNamespace

    from sqlalchemy.exc import OperationalError

    from src.server.api import main

    def err(pgcode):
        return OperationalError("INSERT", {}, SimpleNamespace(pgcode=pgcode))

    assert not main._unavailable(err("54000"))  # program_limit_exceeded
    assert not main._unavailable(err("57014"))  # query_canceled
    assert not main._unavailable(err("53100"))  # disk_full
    assert main._unavailable(err("08006"))  # connection failure
    assert main._unavailable(err("40P01"))  # deadlock
    assert main._unavailable(err("53300"))  # too many connections
    assert main._unavailable(OperationalError("SELECT 1", {}, Exception("could not connect")))  # no SQLSTATE
