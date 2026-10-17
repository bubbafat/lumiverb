# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Choosing who reads a library's files (ADR-018 decision 1, phase 6): this
server or a registered device, never both at once (PUT
/v1/libraries/{id}/reader, or a reader on creating one).

Against the real API and databases (tests/test_scans_api.py's accounts): the
new reader's check-in lists the library and the old one's says it's gone;
only the reader starts, answers and finishes a scan of it, so a scan this
server started before the handover changes nothing (409 not_the_reader);
what's made from previews and analysis proxies is the server's whoever
reads it (the scheduler's queue lists it in every active library), while
what needs the originals is listed only for the libraries the server reads.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from src.server.scheduler.kinds import KINDS
from src.server.scheduler.queue import candidates
from tests.test_device_keys import _check_in, _register
from tests.test_scans_api import (  # noqa: F401 — the shared fixture
    File,
    _error,
    _ingest,
    _library,
    _ok,
    _scan,
    _start,
    env,
)

pytestmark = pytest.mark.slow


def _reader(env, library_id: str, reader: str, **body):
    return env.client.put(f"/v1/libraries/{library_id}/reader", json={"reader": reader, **body}, headers=env.admin)


def _listed(answer: dict) -> list[str]:
    return [lib["library_id"] for lib in answer["libraries"]]


def test_a_library_handed_to_a_device_is_its_alone_and_comes_back(env):
    device_id, device = _register(env, "Handover Mac")
    library_id = _library(env)
    assert library_id in _listed(_ok(_check_in(env, env.admin, library_id, reader="server")))

    handed = _ok(_reader(env, library_id, device_id, root_path="/Users/robert/Pictures/Trips"))
    assert (handed["reader"], handed["root_path"]) == (device_id, "/Users/robert/Pictures/Trips")
    assert handed["reader_name"] == "Handover Mac", "for people: who reads it"
    listed = next(lib for lib in _ok(env.client.get("/v1/libraries", headers=env.viewer))
                  if lib["library_id"] == library_id)
    assert (listed["reader"], listed["reader_name"]) == (device_id, "Handover Mac")
    assert _ok(env.client.get(f"/v1/libraries/{library_id}", headers=env.viewer))["reader_name"] == "Handover Mac"
    server = _ok(_check_in(env, env.admin, library_id, reader="server"))
    assert library_id not in _listed(server) and server["gone"] == [library_id], "this server reads it no more"
    assert library_id in _listed(_ok(_check_in(env, device, library_id)))
    _error(env.client.post(f"/v1/libraries/{library_id}/scans", json={}, headers=env.editor), 409, "not_the_reader")
    run = _scan(env, library_id, [File("a.jpg", "a")], headers=device)
    assert run.finish is not None and set(run.uploaded) == {"a.jpg"}

    back = _ok(_reader(env, library_id, "server", root_path="/Volumes/Trips"))
    assert (back["reader"], back["root_path"], back["reader_name"]) == ("server", "/Volumes/Trips", None)
    assert library_id in _listed(_ok(_check_in(env, env.admin, library_id, reader="server")))
    assert _check_in(env, device, library_id).json()["gone"] == [library_id]
    assert env.client.post(f"/v1/libraries/{library_id}/scans", json={}, headers=device).status_code == 404
    _ok(env.client.post(f"/v1/libraries/{library_id}/scans", json={}, headers=env.editor))


def test_a_scan_started_before_the_handover_changes_nothing(env):
    device_id, _ = _register(env, "Late Mac")
    library_id = _library(env)
    kept = _ingest(env, library_id, File("kept.jpg", "kept"))
    scan_id = _start(env, library_id)
    _ok(env.client.post(f"/v1/scans/{scan_id}/found", json={"files": []}, headers=env.editor))
    _ok(env.client.post(f"/v1/scans/{scan_id}/plan", json={"unlisted": []}, headers=env.editor))
    _ok(_reader(env, library_id, device_id))
    refused = _error(env.client.post(f"/v1/scans/{scan_id}/finish", json={"allow_mass_missing": True},
                                     headers=env.editor), 409, "not_the_reader")
    assert refused["details"] == {"reader": device_id}
    assert _ok(env.client.get(f"/v1/assets/{kept}", headers=env.admin))["asset_id"] == kept, "not taken for gone"
    _ok(_reader(env, library_id, "server"))


def test_a_library_is_made_for_a_device_and_only_a_registered_one(env):
    device_id, device = _register(env, "Hosted Mac")
    made = _ok(env.client.post("/v1/libraries", headers=env.admin, json={
        "name": f"On the Mac {device_id[-6:]}", "root_path": "/Users/robert/Movies", "reader": device_id}))
    assert made["reader"] == device_id
    assert made["library_id"] in _listed(_ok(_check_in(env, device, made["library_id"])))
    _error(env.client.post("/v1/libraries", headers=env.admin, json={
        "name": "Nobody's", "root_path": "/x", "reader": "dev_00000000000000000000000000"}), 404, "device_not_found")
    _error(_reader(env, made["library_id"], "dev_00000000000000000000000000"), 404, "device_not_found")
    _error(_reader(env, made["library_id"], "a laptop"), 404, "device_not_found")
    # Removing the device waits for its libraries to have another reader.
    _error(env.client.delete(f"/v1/devices/{device_id}", headers=env.admin), 409, "device_reads_libraries")
    _ok(_reader(env, made["library_id"], "server"))
    assert env.client.delete(f"/v1/devices/{device_id}", headers=env.admin).status_code == 204


def test_only_an_account_admin_chooses_and_not_for_a_library_in_the_trash(env):
    device_id, device = _register(env, "Chooser Mac")
    library_id = _library(env)
    for headers in (env.editor, env.viewer):
        assert env.client.put(f"/v1/libraries/{library_id}/reader", json={"reader": device_id},
                              headers=headers).status_code == 403
    r = env.client.put(f"/v1/libraries/{library_id}/reader", json={"reader": device_id}, headers=device)
    _error(r, 403, "not_for_devices")
    assert _reader(env, "lib_nothing", "server").status_code == 404
    assert env.client.delete(f"/v1/libraries/{library_id}", headers=env.admin).status_code == 204
    _error(_reader(env, library_id, device_id), 409, "library_trashed")
    assert _reader(env, library_id, "server", root_path="relative/path").status_code == 400


def test_the_database_holds_a_reader_to_this_server_or_a_device(env):
    library_id = _library(env)
    with pytest.raises(IntegrityError), env.db.begin() as conn:
        conn.execute(text("UPDATE libraries SET reader = 'a laptop' WHERE library_id = :l"), {"l": library_id})


def test_work_from_what_s_stored_is_listed_in_every_active_library_and_work_on_originals_only_in_the_server_s(env):
    device_id, _ = _register(env, "Queue Mac")
    ours, theirs, trashed = _library(env), _library(env), _library(env)
    _ok(_reader(env, theirs, device_id))
    photos = {lib: _ingest(env, lib, File(f"{lib}.jpg", f"{lib} photo")) for lib in (ours, theirs, trashed)}
    videos = {lib: _ingest(env, lib, File(f"{lib}.mov", f"{lib} video")) for lib in (ours, theirs)}
    assert env.client.delete(f"/v1/libraries/{trashed}", headers=env.admin).status_code == 204
    with Session(env.db) as session:
        listed = {c["asset_id"] for c in candidates(session, KINDS["clip"], None, limit=10_000)}
        assert {photos[ours], photos[theirs]} <= listed and photos[trashed] not in listed
        on_originals = {c["asset_id"] for c in candidates(session, KINDS["probe"], [ours], limit=10_000)}
        assert videos[ours] in on_originals and videos[theirs] not in on_originals
        with pytest.raises(ValueError):
            candidates(session, KINDS["probe"], None)
