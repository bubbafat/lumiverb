# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A device's key scans the libraries the device reads, and does nothing
else (ADR-018 decision 8, phase 5; src/server/api/devices.py,
routers/devices.py).

Against the real API and databases (tests/test_scans_api.py's two
accounts): an account admin registers a device and gets its key once; the
device checks in, scans its library through every call a scan makes,
sends previews, analysis proxies, probe facts and capture; it gets nothing
of a library it doesn't read and nothing at all from every other route,
which the walk over every route checks. A new key stops the old one; ending
a person's access ends the keys they made.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import text

from src.server.api import devices
from src.server.api.main import app
from src.server.api.middleware import _skip_tenant_middleware
from tests.machine_lineage import made, made_json
from tests.test_scans_api import (  # noqa: F401 — the shared fixture
    File,
    _error,
    _library,
    _ok,
    _scan,
    env,
)
from tests.videos import playable

# Every route a device may call: the scan's, nothing else. A route added here is one more thing a
# device's key can do, so this list changes only with the gate's marks.
SCANNING = {
    ("POST", "/v1/readers/check-in"),
    ("GET", "/v1/libraries/{library_id}/reader-changes"),
    ("GET", "/v1/libraries/{library_id}/reader-work"),
    ("GET", "/v1/libraries/{library_id}/changes"),
    ("POST", "/v1/libraries/{library_id}/changes/ack"),
    ("POST", "/v1/producers/failures"),
    ("POST", "/v1/libraries/{library_id}/scans"),
    ("POST", "/v1/scans/{scan_id}/found"),
    ("POST", "/v1/scans/{scan_id}/plan"),
    ("GET", "/v1/scans/{scan_id}/plan"),
    ("GET", "/v1/scans/{scan_id}/skipped"),
    ("GET", "/v1/scans/{scan_id}/gone"),
    ("POST", "/v1/scans/{scan_id}/hashes"),
    ("POST", "/v1/scans/{scan_id}/moves"),
    ("POST", "/v1/scans/{scan_id}/finish"),
    ("DELETE", "/v1/scans/{scan_id}"),
    ("POST", "/v1/ingest"),
    ("POST", "/v1/assets/{asset_id}/artifacts/{artifact_type}"),
    ("PUT", "/v1/assets/{asset_id}/video-facet"),
    ("PUT", "/v1/assets/{asset_id}/capture"),
    ("POST", "/v1/assets/{asset_id}/uploads"),
    ("GET", "/v1/uploads/{upload_id}"),
    ("PUT", "/v1/uploads/{upload_id}"),
    ("POST", "/v1/uploads/{upload_id}/finish"),
    ("DELETE", "/v1/uploads/{upload_id}"),
}


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _register(env, name: str = "Robert's Mac Studio", headers: dict | None = None) -> tuple[str, dict]:
    device = _ok(env.client.post("/v1/devices", json={"name": name}, headers=headers or env.admin))
    return device["device_id"], _bearer(device["key"])


def _reads(env, library_id: str, reader: str) -> None:
    """The library's reader: a device, or "server". (Choosing one is ADR-018 phase 6's.)"""
    with env.db.begin() as conn:
        conn.execute(text("UPDATE libraries SET reader = :r WHERE library_id = :l"), {"r": reader, "l": library_id})


def _check_in(env, headers: dict, *library_ids: str, **body):
    return env.client.post("/v1/readers/check-in", headers=headers, json={
        "client": "Robert's Mac Studio", "version": "0.1.62", "protocol": 2,
        "libraries": [{"library_id": lib, "state": "idle"} for lib in library_ids], **body})


def _device(env, device_id: str) -> dict:
    return next(d for d in _ok(env.client.get("/v1/devices", headers=env.admin))["devices"]
                if d["device_id"] == device_id)


def _not_for_devices(r) -> None:
    _error(r, 403, "not_for_devices")


@pytest.mark.slow
def test_an_account_admin_registers_a_device_and_sees_its_key_once(env):
    keys_before = {k["key_id"] for k in _ok(env.client.get("/v1/keys", headers=env.admin))["keys"]}
    device = _ok(env.client.post("/v1/devices", json={"name": "  Robert's Mac Studio "}, headers=env.admin))
    assert re.fullmatch(r"dev_[0-9A-Z]{26}", device["device_id"])
    assert device["name"] == "Robert's Mac Studio" and device["has_key"] and device["libraries"] == []
    assert device["key"].startswith("lv_")
    listed = _device(env, device["device_id"])
    assert "key" not in listed and listed["has_key"] and listed["key_last_used_at"] is None
    keys_now = {k["key_id"] for k in _ok(env.client.get("/v1/keys", headers=env.admin))["keys"]}
    assert keys_now == keys_before, "a device's key isn't a person's"
    _ok(_check_in(env, _bearer(device["key"])))
    assert _device(env, device["device_id"])["key_last_used_at"] is not None

    renamed = _ok(env.client.patch(f"/v1/devices/{device['device_id']}", json={"name": "Studio"}, headers=env.admin))
    assert renamed["name"] == "Studio" and "key" not in renamed
    for name in ("", "   ", "a\nb", "x" * 101):
        assert env.client.post("/v1/devices", json={"name": name}, headers=env.admin).status_code == 422, name
    for headers in (env.editor, env.viewer):  # an account admin's
        assert env.client.get("/v1/devices", headers=headers).status_code == 403
        assert env.client.post("/v1/devices", json={"name": "mine"}, headers=headers).status_code == 403
    assert env.client.patch("/v1/devices/dev_nope", json={"name": "x"}, headers=env.admin).status_code == 404
    assert env.client.post("/v1/devices/dev_nope/key", headers=env.admin).status_code == 404
    other = _ok(env.client.get("/v1/devices", headers=env.other))["devices"]
    assert device["device_id"] not in {d["device_id"] for d in other}, "another account's devices are its own"


@pytest.mark.slow
def test_a_device_scans_the_library_it_reads(env):
    device_id, device = _register(env)
    library_id, theirs = _library(env), _library(env)
    _reads(env, library_id, device_id)

    checked = _ok(_check_in(env, device, library_id, theirs))
    assert [(lib["library_id"], lib["reader"]) for lib in checked["libraries"]] == [(library_id, device_id)]
    assert checked["gone"] == [theirs], "a library it doesn't read"
    assert _ok(env.client.get(f"/v1/libraries/{library_id}/reader-changes", headers=device))["library"]["reader"] == device_id
    _ok(env.client.get(f"/v1/libraries/{library_id}/reader-work", headers=device))
    assert _device(env, device_id)["libraries"] == [library_id]

    disk = [File("a/photo.jpg", "photo"), File("a/clip.mov", "clip")]
    run = _scan(env, library_id, disk, headers=device)
    assert run.finish is not None and set(run.uploaded) == {"a/photo.jpg", "a/clip.mov"}
    again = _scan(env, library_id, disk, headers=device)
    assert again.counts.get("upload", 0) == 0 and not again.uploaded, "what it sent is there"
    clip = run.uploaded["a/clip.mov"]

    # What it makes from the originals: probe facts, capture, an analysis proxy in chunks.
    assert env.client.put(f"/v1/assets/{clip}/video-facet", json={}, headers=device).status_code == 422  # by the gate
    _ok(env.client.put(f"/v1/assets/{clip}/capture", json={"lineage": made("lumiverb.capture_time")}, headers=device))
    data = playable(2.0)
    upload = _ok(env.client.post(f"/v1/assets/{clip}/uploads", headers=device, json={
        "artifact": "lumiverb.video.analysis_proxy", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made("lumiverb.video.analysis_proxy")}))
    _ok(env.client.put(upload["chunk_url"], params={"offset": 0}, content=data,
                       headers={**device, "Content-Type": "application/octet-stream"}))
    assert _ok(env.client.get(upload["chunk_url"], headers=device))["received"] == len(data)
    assert _ok(env.client.post(f"{upload['chunk_url']}/finish", headers=device))["duration_sec"] > 1.5

    # What it doesn't make, it doesn't send.
    r = env.client.post(f"/v1/assets/{clip}/artifacts/scene_rep", headers=device,
                        files={"file": ("f.jpg", b"jpeg", "image/jpeg")})
    _not_for_devices(r)
    # Its own clips are a scan's to send, not its to see.
    _not_for_devices(env.client.get(f"/v1/assets/{clip}", headers=device))
    _not_for_devices(env.client.get(f"/v1/assets/{clip}/artifacts/proxy", headers=device))
    _not_for_devices(env.client.get("/v1/query", params={"f": f"library:{library_id}"}, headers=device))
    _not_for_devices(env.client.post(f"/v1/libraries/{library_id}/scan-now", headers=device))


@pytest.mark.slow
def test_a_device_gets_nothing_of_a_library_it_doesnt_read(env):
    device_id, device = _register(env)
    _, others = _register(env, "Another computer")
    mine, theirs = _library(env), _library(env)  # theirs: this server's
    _reads(env, mine, device_id)
    clip = _scan(env, theirs, [File("v.mov", "theirs")]).uploaded["v.mov"]
    scan_id = _ok(env.client.post(f"/v1/libraries/{theirs}/scans", json={}, headers=env.editor))["scan_id"]
    data = playable()
    upload = _ok(env.client.post(f"/v1/assets/{clip}/uploads", headers=env.editor, json={
        "artifact": "lumiverb.video.preview", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "lineage": made("lumiverb.video.preview")}))

    for headers in (device, others):  # this server's library; and mine is only mine
        library = theirs if headers is device else mine
        assert env.client.post(f"/v1/libraries/{library}/scans", json={}, headers=headers).status_code == 404
        assert env.client.get(f"/v1/libraries/{library}/reader-changes", headers=headers).status_code == 404
    assert env.client.post(f"/v1/scans/{scan_id}/found", json={"files": []}, headers=device).status_code == 404
    assert env.client.delete(f"/v1/scans/{scan_id}", headers=device).status_code == 404
    assert env.client.put(f"/v1/assets/{clip}/capture", json={"lineage": made("lumiverb.capture_time")},
                          headers=device).status_code == 404
    assert env.client.post(f"/v1/assets/{clip}/artifacts/video_preview", headers=device,
                           files={"file": ("p.mp4", data, "video/mp4")},
                           data={"lineage": "{}"}).status_code == 404
    assert env.client.get(upload["chunk_url"], headers=device).status_code == 404
    assert env.client.delete(upload["chunk_url"], headers=device).status_code == 404
    r = env.client.post("/v1/ingest", headers=device, files={"proxy": ("p.jpg", b"jpeg", "image/jpeg")},
                        data={"library_id": theirs, "rel_path": "new.jpg"})
    assert r.status_code == 404, r.text
    assert env.client.get(upload["chunk_url"], headers=env.editor).status_code == 200, "untouched"
    assert _ok(env.client.post(f"/v1/scans/{scan_id}/found", json={"files": []}, headers=env.editor)) is not None


@pytest.mark.slow
def test_every_other_route_turns_a_device_away(env):
    """The gate before every route: a device's key reaches the scanning
    routes alone, and a route added later turns it away until it says
    otherwise. Where the middleware takes no account's key (an operator's,
    sign-in, signed links), the key gets what no key gets."""
    from fastapi.routing import APIRoute, iter_route_contexts

    _, device = _register(env)
    marked, refused = set(), 0
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for method in sorted(set(context.methods or ()) - {"HEAD", "OPTIONS"}):
            path = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: "proxy" if m.group(1) == "artifact_type" else "nope",
                          context.path)
            if devices.marked(context.original_route.endpoint) is not None:
                marked.add((method, context.path))
                r = env.client.request(method, path, headers=device)
                assert r.status_code != 403, (method, context.path, r.text)
            elif _skip_tenant_middleware(context.path):  # upkeep takes an admin's key, never a device's
                if env.client.request(method, path, headers=device).status_code < 400:
                    assert env.client.request(method, path).status_code < 400, (method, context.path)
            else:
                r = env.client.request(method, path, headers=device)
                assert (r.status_code, r.json()["error"]["code"]) == (403, "not_for_devices"), (method, context.path)
                refused += 1
    assert marked == SCANNING
    assert refused > 100


@pytest.mark.slow
def test_a_new_key_stops_the_old_one_at_once(env):
    device_id, first = _register(env)
    second = _ok(env.client.post(f"/v1/devices/{device_id}/key", headers=env.admin))
    assert second["device_id"] == device_id and second["has_key"]
    _error(_check_in(env, first), 401, "unauthorized")
    _ok(_check_in(env, _bearer(second["key"])))
    assert env.client.delete(f"/v1/devices/{device_id}/key", headers=env.admin).status_code == 204
    _error(_check_in(env, _bearer(second["key"])), 401, "unauthorized")
    assert not _device(env, device_id)["has_key"]
    third = _ok(env.client.post(f"/v1/devices/{device_id}/key", headers=env.admin))
    _ok(_check_in(env, _bearer(third["key"])))


@pytest.mark.slow
def test_a_device_is_removed_once_no_library_names_it(env):
    device_id, device = _register(env)
    library_id = _library(env)
    _reads(env, library_id, device_id)
    error = _error(env.client.delete(f"/v1/devices/{device_id}", headers=env.admin), 409, "device_reads_libraries")
    assert [lib["library_id"] for lib in error["details"]["libraries"]] == [library_id]
    _ok(_check_in(env, device))  # still its reader, still working
    _reads(env, library_id, "server")
    assert env.client.delete(f"/v1/devices/{device_id}", headers=env.admin).status_code == 204
    _error(_check_in(env, device), 401, "unauthorized")
    assert device_id not in {d["device_id"] for d in _ok(env.client.get("/v1/devices", headers=env.admin))["devices"]}
    assert env.client.delete(f"/v1/devices/{device_id}", headers=env.admin).status_code == 404


@pytest.mark.slow
def test_the_keys_a_person_made_end_with_their_access(env):
    from src.server.api.rate_limit import login_limiter

    email = "device-admin@example.com"
    for who in ("keeper@example.com", email):  # never the account's last admin
        r = env.client.post("/v1/users", json={"email": who, "password": "correct horse battery", "role": "admin"},
                            headers=env.admin)
        assert r.status_code == 201, r.text
    login_limiter._hits.clear()
    token = _ok(env.client.post("/v1/auth/login", json={"email": email, "password": "correct horse battery"}))
    device_id, device = _register(env, "Their laptop", headers=_bearer(token["access_token"]))
    _ok(_check_in(env, device))
    assert env.client.delete(f"/v1/users/{r.json()['user_id']}", headers=env.admin).status_code == 204
    _error(_check_in(env, device), 401, "unauthorized")
    assert not _device(env, device_id)["has_key"]  # the device stays: an admin gives it a new key


@pytest.mark.slow
def test_a_device_says_nothing_of_who_it_is(env):
    """Its key says which device it is: an admin's key can't check in as one."""
    device_id, _ = _register(env)
    library_id = _library(env)
    _reads(env, library_id, device_id)
    assert _check_in(env, env.admin, library_id, reader=device_id).status_code == 422
    assert _ok(_check_in(env, env.admin, library_id))["gone"] == [library_id], "this server doesn't read it"


@pytest.mark.slow
def test_a_device_sends_by_id_or_by_name(env):
    # ADR-024 phase 1: the gate takes an artifact's name and a file's (src/producers/names.py) as it takes the id.
    import io

    from PIL import Image

    def jpeg() -> tuple:
        buf = io.BytesIO()
        Image.new("RGB", (64, 36)).save(buf, format="JPEG")
        return ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")

    device_id, device = _register(env, "Names")
    library_id = _library(env)
    _reads(env, library_id, device_id)
    run = _scan(env, library_id, [File("n/photo.jpg", "named photo"), File("n/clip.mov", "named clip")], headers=device)
    photo, clip = run.uploaded["n/photo.jpg"], run.uploaded["n/clip.mov"]
    for artifact in ("probe", "lumiverb.video.format"):
        assert _ok(env.client.post("/v1/producers/failures", headers=device, json={"items": [
            {"asset_id": clip, "artifact": artifact, "error": "unreadable"}]}))["recorded"] == 1, artifact
    _not_for_devices(env.client.post("/v1/producers/failures", headers=device, json={"items": [
        {"asset_id": photo, "artifact": "lumiverb.image.description", "error": "unreadable"}]}))
    for kind in ("proxy", "lumiverb.preview"):
        _ok(env.client.post(f"/v1/assets/{photo}/artifacts/{kind}", headers=device, files={"file": jpeg()},
                            data={"lineage": made_json("lumiverb.preview")}))
    _not_for_devices(env.client.post(f"/v1/assets/{clip}/artifacts/lumiverb.video.scenes.picture", headers=device,
                                     files={"file": jpeg()}))
