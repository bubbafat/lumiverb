# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A library's reader checks in, and a mark per library tells it when to
fetch what changed (ADR-018 decision 4, phase 4; src/server/check_ins.py).

Against the real API and database: what a check-in keeps and answers, what
moves a mark (the database's triggers) and what doesn't, what a reader
fetches when it moves, and a scan asked of the reader (Scan now) until a
scan of the whole library answers it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want


def _library(env, name: str) -> tuple:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/tmp/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    return (client, headers, r.json()["library_id"], *env[3:])


def _check_in(env, libraries: list[dict] | None = None, **body) -> dict:
    client, headers, *_ = env
    r = client.post("/v1/readers/check-in", json={"client": "this server", "version": "0.1.test", "protocol": 1,
                                                  "libraries": libraries or [], **body}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _mark(env) -> int:
    library_id = env[2]
    return {lib["library_id"]: lib["mark"] for lib in _check_in(env)["libraries"]}[library_id]


def _listed(env) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/libraries", headers=headers)
    assert r.status_code == 200, r.text
    return {lib["library_id"]: lib for lib in r.json()}[library_id]


def _ok(r, status: int = 200) -> dict:
    assert r.status_code == status, r.text
    return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# The check-in
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_check_in_is_kept_and_answered_with_the_libraries_it_reads(env):
    a = _library(env, "CheckInA")
    b = _library(env, "CheckInB")
    answer = _check_in(env, [{"library_id": a[2], "state": "idle", "last_full_scan_at": "2026-10-17T09:00:00Z",
                              "mark": 0},
                             {"library_id": "lib_nope", "state": "idle"}])
    listed = {lib["library_id"]: lib for lib in answer["libraries"]}
    assert {a[2], b[2]} <= set(listed)  # this server reads every library
    assert listed[a[2]] | {"mark": None} == {"library_id": a[2], "name": "CheckInA", "root_path": "/tmp/CheckInA",
                                             "reader": "server", "scans_paused": False, "mark": None,
                                             "scan_now": False}
    assert answer["gone"] == ["lib_nope"] and answer["check_in_every_sec"] == 60
    assert [lib["name"] for lib in answer["libraries"]] == sorted(lib["name"] for lib in answer["libraries"])

    seen = _listed(a)["checked_in"]
    assert seen | {"checked_in_at": None} == {"client": "this server", "version": "0.1.test", "state": "idle",
                                              "waiting_files": 0, "waiting_bytes": 0, "checked_in_at": None,
                                              "last_full_scan_at": "2026-10-17T09:00:00+00:00"}
    assert _listed(b)["checked_in"] is None  # never said anything of it

    _check_in(env, [{"library_id": a[2], "state": "scanning", "waiting_files": 3, "waiting_bytes": 4096}])
    seen = _listed(a)["checked_in"]
    assert (seen["state"], seen["waiting_files"], seen["waiting_bytes"], seen["last_full_scan_at"]) == (
        "scanning", 3, 4096, None)


@pytest.mark.slow
def test_a_library_in_the_trash_is_gone_from_its_reader(env):
    client, headers, *_ = env
    lib = _library(env, "CheckInTrashed")
    _ok(client.delete(f"/v1/libraries/{lib[2]}", headers=headers), 204)
    answer = _check_in(env, [{"library_id": lib[2], "state": "idle"}])
    assert lib[2] not in {x["library_id"] for x in answer["libraries"]} and answer["gone"] == [lib[2]]


@pytest.mark.slow
def test_only_an_admin_checks_in_as_this_server(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, *_ = env
    body = {"client": "x", "version": "1", "protocol": 1, "libraries": []}
    for role in ("viewer", "editor"):
        assert client.post("/v1/readers/check-in", json=body, headers=_key_with_role(env, role)).status_code == 403
    assert client.post("/v1/readers/check-in", json={**body, "reader": "device:x"},
                       headers=env[1]).status_code == 422  # devices come with ADR-018 phase 6
    bad = {**body, "libraries": [{"library_id": "lib_x", "state": "asleep"}]}
    assert client.post("/v1/readers/check-in", json=bad, headers=env[1]).status_code == 422


@pytest.mark.slow
def test_a_reader_too_old_is_told_so(env, monkeypatch):
    from src.server import check_ins

    client, headers, *_ = env
    monkeypatch.setattr(check_ins, "OLDEST_PROTOCOL", 2)
    r = client.post("/v1/readers/check-in", json={"client": "x", "version": "1", "protocol": 1}, headers=headers)
    assert r.status_code == 426 and r.json()["error"]["code"] == "reader_too_old", r.text


# ---------------------------------------------------------------------------
# The mark
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_the_mark_moves_with_what_the_reader_acts_on(env):
    client, headers, *_ = env
    lib = _library(env, "MarkMoves")
    library_id = lib[2]
    clip = _ingest_with(lib, "a.jpg", _sha())

    def moves(what: str, change) -> None:
        before = _mark(lib)
        change()
        assert _mark(lib) > before, what

    def ok(r, status: int = 200) -> dict:
        return _ok(r, status)

    filter_id = {}

    def add_filter():
        filter_id["id"] = ok(client.post(f"/v1/libraries/{library_id}/filters",
                                         json={"type": "exclude", "pattern": "**/tmp/**"}, headers=headers), 201)["filter_id"]

    moves("its own filter", add_filter)
    moves("its filter removed", lambda: ok(client.delete(f"/v1/libraries/{library_id}/filters/{filter_id['id']}",
                                                         headers=headers), 204))
    default = {}

    def add_default():
        default["id"] = ok(client.post("/v1/tenant/filter-defaults", json={"type": "exclude", "pattern": "**/.cache/**"},
                                       headers=headers), 201)["default_id"]

    moves("the account's filter", add_default)
    moves("the account's filter removed",
          lambda: ok(client.delete(f"/v1/tenant/filter-defaults/{default['id']}", headers=headers), 204))
    moves("its scans paused", lambda: ok(client.post("/v1/libraries/scans/pause", json={"library_ids": [library_id]},
                                                     headers=headers), 204))
    moves("its scans resumed", lambda: ok(client.post("/v1/libraries/scans/resume", json={"library_ids": [library_id]},
                                                      headers=headers), 204))
    moves("a producer paused", lambda: ok(client.post("/v1/producers/probe/pause", json={"scope": "work"},
                                                      headers=headers), 204))
    moves("a producer resumed", lambda: ok(client.post("/v1/producers/probe/resume", json={"scope": "work"},
                                                       headers=headers), 204))

    def settings(crf):
        ok(client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": crf}, "redo": True},
                      headers=headers))

    moves("what it makes is made another way", lambda: settings(30))
    moves("and back", lambda: settings(None))
    moves("its root moved", lambda: ok(client.patch(f"/v1/libraries/{library_id}", json={"root_path": "/tmp/moved"},
                                                    headers=headers)))
    ok(client.post("/v1/producers/failures", json={"items": [{"asset_id": clip, "artifact": "capture",
                                                              "error": "exiftool couldn't read it"}]},
                   headers=headers))
    moves("failing clips asked to be tried again", lambda: ok(client.post("/v1/producers/failures/retry", json={"all": True},
                                                                         headers=headers)))
    moves("a scan asked for", lambda: ok(client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers), 204))

    def lost():
        with _db(lib) as s:  # as the missing-file repair clears a lost preview's key
            s.execute(text("UPDATE assets SET proxy_key = NULL WHERE asset_id = :a"), {"a": clip})
            s.commit()

    moves("a preview lost", lost)


@pytest.mark.slow
def test_the_mark_stays_for_what_the_reader_made_or_needs_nothing_of(env):
    client, headers, *_ = env
    lib = _library(env, "MarkStays")
    library_id = lib[2]
    before = _mark(lib)
    _ingest_with(lib, "a.jpg", _sha())  # its own scan's upload: it knows
    _ok(client.patch(f"/v1/libraries/{library_id}", json={"name": "MarkStill"}, headers=headers))
    with _db(lib) as s:  # the scheduler's status, every few seconds
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', '{}', now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"))
        s.commit()
    _check_in(lib, [{"library_id": library_id, "state": "idle", "mark": before}])
    assert _mark(lib) == before


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_what_changed_is_everything_a_reader_acts_on(env):
    client, headers, *_ = env
    lib = _library(env, "Changes")
    library_id = lib[2]
    _ok(client.post(f"/v1/libraries/{library_id}/filters", json={"type": "exclude", "pattern": "**/Trash/**"},
                    headers=headers), 201)
    _ok(client.post(f"/v1/libraries/{library_id}/filters", json={"type": "include", "pattern": "Footage/**"},
                    headers=headers), 201)
    _ok(client.post("/v1/producers/capture/pause", json={"scope": "work"}, headers=headers), 204)
    try:
        r = _ok(client.get(f"/v1/libraries/{library_id}/reader-changes", params={"since": 5}, headers=headers))
        mark = _mark(lib)
    finally:
        _ok(client.post("/v1/producers/capture/resume", json={"scope": "work"}, headers=headers), 204)
    assert r["everything"] is True and r["mark"] == mark
    assert r["library"] == {"library_id": library_id, "name": "Changes", "root_path": "/tmp/Changes",
                            "reader": "server", "status": "active", "scans_paused": False, "scan_now": False}
    assert r["filters"]["library"] == {"includes": ["Footage/**"], "excludes": ["**/Trash/**"]}
    defaults = _ok(client.get("/v1/tenant/filter-defaults", headers=headers))
    assert r["filters"]["account"] == {"includes": sorted(d["pattern"] for d in defaults["includes"]),
                                       "excludes": sorted(d["pattern"] for d in defaults["excludes"])}
    assert set(r["made_with"]) == {"proxy", "video_preview", "probe", "capture", "analysis_proxy"}
    for artifact, made in r["made_with"].items():
        want = _want(lib, artifact, None)
        assert (made["producer"], made["version"], made["settings_hash"]) == (
            want["producer"], want["version"], want["settings_hash"]), artifact
    assert r["paused"] == {"work": ["capture"], "remakes": []}
    assert client.get("/v1/libraries/lib_nope/reader-changes", headers=headers).status_code == 404


@pytest.mark.slow
def test_a_scan_takes_its_filters_from_what_changed(env, monkeypatch, tmp_path):
    # What the scheduler's scans and `lumiverb scan` exercise every time.
    from src.processing.scan import _filters
    from src.shared.path_filter import PathFilter

    client, headers, *_ = env
    lib = _library(env, "ScanFilters")
    _ok(client.post(f"/v1/libraries/{lib[2]}/filters", json={"type": "exclude", "pattern": "**/Proxy/**"},
                    headers=headers), 201)

    class Client:
        def get(self, path, **kw):
            return client.get(path, headers=headers, **kw)

    account, library = _filters(Client(), lib[2])
    assert library == [PathFilter(type="exclude", pattern="**/Proxy/**")]
    assert all(isinstance(f, PathFilter) for f in account)


# ---------------------------------------------------------------------------
# Scan now
# ---------------------------------------------------------------------------


def _whole_scan(env, **opts) -> None:
    """A scan of the library as a reader makes one, with nothing found (the library has no clips)."""
    client, headers, library_id, *_ = env
    scan_id = _ok(client.post(f"/v1/libraries/{library_id}/scans", json=opts, headers=headers))["scan_id"]
    _ok(client.post(f"/v1/scans/{scan_id}/plan", json={}, headers=headers))
    _ok(client.post(f"/v1/scans/{scan_id}/finish", json={}, headers=headers))


@pytest.mark.slow
def test_a_scan_asked_for_is_answered_by_a_scan_of_the_whole_library(env):
    client, headers, *_ = env
    lib = _library(env, "ScanNow")
    library_id = lib[2]
    _ok(client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers), 204)
    asked = _listed(lib)["scan_requested_at"]
    assert asked is not None
    _ok(client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers), 204)
    assert _listed(lib)["scan_requested_at"] == asked  # the first ask stands

    def scan_now() -> bool:
        return {x["library_id"]: x["scan_now"] for x in _check_in(lib)["libraries"]}[library_id]

    assert scan_now()
    _whole_scan(lib, scope="Footage")  # a folder isn't the whole library
    _whole_scan(lib, media_type="video")  # nor is one media type
    _whole_scan(lib, dry_run=True)  # nor a scan that changes nothing
    assert scan_now()
    _whole_scan(lib)
    assert not scan_now() and _listed(lib)["scan_requested_at"] is None


@pytest.mark.slow
def test_a_scan_started_before_the_ask_does_not_answer_it(env):
    client, headers, *_ = env
    lib = _library(env, "ScanNowLate")
    library_id = lib[2]
    scan_id = _ok(client.post(f"/v1/libraries/{library_id}/scans", json={}, headers=headers))["scan_id"]
    _ok(client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers), 204)
    _ok(client.post(f"/v1/scans/{scan_id}/plan", json={}, headers=headers))
    _ok(client.post(f"/v1/scans/{scan_id}/finish", json={}, headers=headers))
    assert _listed(lib)["scan_requested_at"] is not None


@pytest.mark.slow
def test_who_can_ask_for_a_scan_and_when(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, headers, *_ = env
    lib = _library(env, "ScanNowWho")
    library_id = lib[2]
    assert client.post(f"/v1/libraries/{library_id}/scan-now", headers=_key_with_role(env, "viewer")).status_code == 403
    assert client.post(f"/v1/libraries/{library_id}/scan-now", headers=_key_with_role(env, "editor")).status_code == 204
    assert client.post("/v1/libraries/lib_nope/scan-now", headers=headers).status_code == 404
    _ok(client.post("/v1/libraries/scans/pause", json={"library_ids": [library_id]}, headers=headers), 204)
    try:
        r = client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "scans_paused", r.text
    finally:
        _ok(client.post("/v1/libraries/scans/resume", json={"library_ids": [library_id]}, headers=headers), 204)
    _ok(client.delete(f"/v1/libraries/{library_id}", headers=headers), 204)
    r = client.post(f"/v1/libraries/{library_id}/scan-now", headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "library_trashed", r.text
