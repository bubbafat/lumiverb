# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Crashes charged after so many in a row; a new producer version's redo
waits for an admin (review, Oct 9).

A job that crashes charges no clip, but each clip's crashes in a row are
kept (producer_crashes): the CRASHES_BEFORE_CHARGE-th is charged, so the
back-off and giving up apply. A success or a charged failure starts it over.

A producer's new version is like new settings nobody approved: the first
time the scheduler sees clips an older version made, the producer's redo is
stopped; Processing shows it stopped with the stale count, and an admin
resumes it. New defaults a release brings (a prompt) are held the same way
when they change what the account makes it with.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from src.server.repository import lineage
from src.server.scheduler.kinds import KINDS
from src.server.scheduler.service import _crashed_in_database, _from_database
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _library
from tests.derived import saved

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
        assert _crashed_in_database(tenant_id, "lumiverb.image.text", [clip, "ast_gone"], "boom") == []
        assert _crashes(lib, clip, "lumiverb.image.text") == n
    assert _crashed_in_database(tenant_id, "lumiverb.image.text", [clip], "boom") == [clip]
    assert _crashes(lib, clip, "lumiverb.image.text") is None  # starts over
    assert _crashes(lib, "ast_gone", "lumiverb.image.text") is None  # a clip that's gone isn't kept


def test_a_success_or_a_charged_failure_starts_the_count_over(env):
    lib = _library(env, "CrashesReset")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    tenant_id = env[4]
    _crashed_in_database(tenant_id, "lumiverb.image.description", [clip], "boom")
    _crashed_in_database(tenant_id, "lumiverb.image.text", [clip], "boom")
    r = saved(client, "vision", clip, json={"model_id": "m", "description": "a dog",
                                                        "lineage": _want(lib, "lumiverb.image.description", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _crashes(lib, clip, "lumiverb.image.description") is None
    r = client.post("/v1/producers/failures", json={"items": [{"asset_id": clip, "artifact": "lumiverb.image.text", "error": "x"}]},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _crashes(lib, clip, "lumiverb.image.text") is None


def _made_by_an_older_version(lib, name: str) -> str:
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, name, sha)
    r = saved(client, "vision", clip, json={"model_id": "m", "description": "old words",
                                                        "lineage": _want(lib, "lumiverb.image.description", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(lib) as s:  # as if vision's version went up since
        s.execute(text("UPDATE artifact_lineage SET producer_version = '0' WHERE asset_id = :a AND artifact = 'lumiverb.image.description'"),
                  {"a": clip})
        s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.lumiverb.image.description.version_seen'"))
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
    redo = KINDS["redo:lumiverb.image.description"]
    try:
        assert _from_database(env[4], redo, [library_id]) is None  # stopped, not handed out
        r = client.get("/v1/producers", headers=headers)
        p = {p["artifact"]: p for p in r.json()["producers"]}["lumiverb.image.description"]
        assert p["redo_stopped"] is True and p["counts"]["stale"] >= 1  # what Processing shows
        assert _from_database(env[4], redo, [library_id]) is None  # still stopped
        assert client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": "redo"}, headers=headers).status_code == 204
        assert clip in [i["asset_id"] for i in _from_database(env[4], redo, [library_id])]  # resuming sticks
    finally:
        client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": "redo"}, headers=headers)


def test_a_new_versions_hold_is_found_while_its_producer_is_paused(env):
    """Oct 12: the hold was made only when its redo was listed, so not while
    the producer was paused, and Processing couldn't show it. The scheduler's
    refill looks for it, whatever is paused, before any redo is listed."""
    from unittest.mock import MagicMock

    from src.server.scheduler.service import Scheduler
    from tests.test_scheduler_service import FakeAccount

    lib = _library(env, "NewVersionPaused")
    client, headers, library_id, *_ = lib
    _made_by_an_older_version(lib, "a.jpg")
    try:
        assert client.post("/v1/producers/lumiverb.image.description/pause", json={"scope": "work"}, headers=headers).status_code == 204
        # No AI machines and no slots: nothing runs, only what's due is listed.
        acct = FakeAccount(env[4], vision=0, transcripts=0, clip=0, faces=0, libraries=(library_id,), reachable=(library_id,))
        Scheduler(lambda: {env[4]: acct}, capacity={}, scan=MagicMock(), inline_refill=True).tick()
        p = {p["artifact"]: p for p in client.get("/v1/producers", headers=headers).json()["producers"]}["lumiverb.image.description"]
        assert p["paused"] is True and p["redo_stopped"] is True and p["redo_stopped_by"] is None
    finally:
        client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": "work"}, headers=headers)
        client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": "redo"}, headers=headers)


def test_without_clips_an_older_version_made_nothing_is_stopped(env):
    lib = _library(env, "SameVersion")
    with _db(lib) as s:
        s.execute(text("UPDATE artifact_lineage SET producer_version = :v WHERE artifact = 'lumiverb.image.description'"),
                  {"v": lineage.PRODUCERS["lumiverb.image.description"].version})
        s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.lumiverb.image.description.version_seen'"))
        s.commit()
        assert lineage.hold_new_version(s, "lumiverb.image.description") is False
        assert "lumiverb.image.description" not in lineage.pauses(s).redo


DESCRIPTION = "lumiverb.image.description"
DEFAULTS_SEEN = f"producer.{DESCRIPTION}.defaults_seen"


def _made_the_old_way(lib, name: str, seen: dict[str, str] | None) -> str:
    """A clip described by this version with settings another than now's (its hash "made-before"), and what was
    seen of the producer's settings at the scheduler's last look (seen: {defaults, desired}, else nothing yet)."""
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, name, sha)
    r = saved(client, "vision", clip, json={"model_id": "m", "description": "old words",
                                            "lineage": _want(lib, DESCRIPTION, sha)}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(lib) as s:
        s.execute(text("UPDATE artifact_lineage SET settings_hash = 'made-before' WHERE asset_id = :a"
                       " AND artifact = :b"), {"a": clip, "b": DESCRIPTION})
        s.execute(text("DELETE FROM system_metadata WHERE key = :k"), {"k": DEFAULTS_SEEN})
        if seen is not None:
            s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, :v, now())"),
                      {"k": DEFAULTS_SEEN, "v": json.dumps(seen)})
        s.commit()
    return clip


def _defaults_now() -> str:
    from src.shared.producers import effective_settings, settings_hash

    return settings_hash(effective_settings(DESCRIPTION))


def test_new_defaults_a_release_brings_wait_for_an_admin_to_resume_their_redo(env):
    """The demo server, 10-21: an update changed the video summaries' default prompt, and every summary was made
    again through a paid AI machine within seconds, nobody asked. A release's new defaults are held as its new
    version is."""
    from tests.test_redo_on_change import VISION

    _set_model(env, "vision", VISION)
    lib = _library(env, "NewDefaults")
    client, headers, library_id, *_ = lib
    # Seen before the release: other defaults, and what the account made then (made-before).
    clip = _made_the_old_way(lib, "a.jpg", {"defaults": "the old prompt's", "desired": "made-before"})
    redo = KINDS[f"redo:{DESCRIPTION}"]
    try:
        with _db(lib) as s:  # what a scan's plan and the reader's list ask before the scheduler has looked
            assert lineage.new_defaults_unseen(s, DESCRIPTION, {"vision": VISION}) is True
        assert _from_database(env[4], redo, [library_id]) is None  # stopped, not handed out
        with _db(lib) as s:
            assert lineage.new_defaults_unseen(s, DESCRIPTION, {"vision": VISION}) is False, "seen now"
        r = client.get("/v1/producers", params={"library_id": library_id}, headers=headers)
        p = {p["artifact"]: p for p in r.json()["producers"]}[DESCRIPTION]
        assert p["redo_stopped"] is True and p["new_version"] is True and p["redo_stopped_by"] is None
        assert {"reason": "new_version", "clips": 1} in p["waits"] and p["counts"]["stale"] == 1  # what it says
        assert _from_database(env[4], redo, [library_id]) is None  # still stopped: seen once, not asked again
        assert client.post(f"/v1/producers/{DESCRIPTION}/resume", json={"scope": "redo"}, headers=headers).status_code == 204
        assert clip in [i["asset_id"] for i in _from_database(env[4], redo, [library_id])]  # resuming sticks
    finally:
        client.post(f"/v1/producers/{DESCRIPTION}/resume", json={"scope": "redo"}, headers=headers)


@pytest.mark.parametrize("seen", [
    None,  # the first look: nothing seen yet
    "a person's change",  # the same defaults: a person changed what the account makes, and was asked then
    "the account's own",  # new defaults, but the account makes it with its own settings: nothing changed for it
], ids=["first_look", "persons_change", "accounts_own"])
def test_what_no_release_changed_for_the_account_holds_nothing(env, seen):
    from src.server.scheduler.service import _hold_new_versions_in_database
    from tests.test_redo_on_change import VISION

    _set_model(env, "vision", VISION)
    lib = _library(env, f"Unheld {seen}")
    client, headers, library_id, *_ = lib
    desired_now = _want(lib, DESCRIPTION, None)["settings_hash"]
    clip = _made_the_old_way(lib, "a.jpg", None if seen is None else {
        "defaults": _defaults_now() if seen == "a person's change" else "the old prompt's",
        "desired": "made-before" if seen == "a person's change" else desired_now})
    _hold_new_versions_in_database(env[4])
    with _db(lib) as s:
        assert DESCRIPTION not in lineage.pauses(s).redo
        assert json.loads(s.execute(text("SELECT value FROM system_metadata WHERE key = :k"),
                                    {"k": DEFAULTS_SEEN}).scalar()) == {"defaults": _defaults_now(),
                                                                         "desired": desired_now}, "seen now"
    assert clip in [i["asset_id"] for i in _from_database(env[4], KINDS[f"redo:{DESCRIPTION}"], [library_id])]


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
