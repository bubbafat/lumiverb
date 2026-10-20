# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Retired endpoints answer 410 gone, naming what replaced them.

The old scan protocol's (ADR-018 decision 12): scans compare on the server
now, a scan session deciding what's new, changed, moved and skipped
(POST /v1/libraries/{id}/scans, routers/scans.py), and a file becomes a
clip when it's ingested.

The Mac app's own AI enrichment's (removed with its scan): the server's
scheduler does all processing, finding what each producer still needs in
the database (scheduler/queue.py) and saving a photo's faces with
POST /v1/assets/batch-faces; clients list a library's clips with
GET /v1/query.

Each stays defined, behind the role it always needed, and answers 410 in
the error envelope.
"""

from __future__ import annotations

import uuid

import pytest

from tests.machine_lineage import made
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow

SCANS = "POST /v1/libraries/{library_id}/scans"
SCAN_SAYS = ("retired", "scan session", "ADR-018")
# (method, path, what replaced it, what its message says)
RETIRED = [
    ("POST", "/v1/assets/upsert", [SCANS, "POST /v1/ingest"], SCAN_SAYS),
    ("POST", "/v1/assets/batch-moves", [SCANS], SCAN_SAYS),
    ("GET", "/v1/libraries/{library_id}/ignored-paths", [SCANS], SCAN_SAYS),
    ("GET", "/v1/assets/page", ["GET /v1/query"], ("retired", "GET /v1/query", "scheduler")),
    ("POST", "/v1/assets/{asset_id}/faces", ["POST /v1/assets/batch-faces", "POST /v1/video/{asset_id}/faces"],
     ("retired", "scheduler", "POST /v1/assets/batch-faces")),
]
IDS = [f"{method} {path}" for method, path, *_ in RETIRED]
FACE = {"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "detection_confidence": 0.9,
        "embedding": [0.1] * 512}


def _key(env, role: str) -> dict:
    client, headers, *_ = env
    r = client.post("/v1/keys", json={"label": f"retired-{role}", "role": role}, headers=headers)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['plaintext']}"}


def _as_an_old_client(env, method: str, path: str, headers: dict | None, asset_id: str | None = None):
    """The request an old client sent: an upsert's file, a scan's move, the
    list of removed paths, a page of clips missing faces, or a photo's faces."""
    client, _, library_id, *_ = env
    body = params = None
    if path.endswith("/upsert"):
        body = {"library_id": library_id, "rel_path": "retired/a.jpg", "file_size": 1, "file_mtime": None,
                "media_type": "image"}
    elif path.endswith("/batch-moves"):
        body = {"items": [{"asset_id": _ingest(env, f"retired/{uuid.uuid4().hex[:8]}.mov"),
                           "rel_path": "retired/moved.mov"}]}
    elif path.endswith("/page"):
        params = {"library_id": library_id, "missing_faces": "true", "limit": 100}
    elif path.endswith("/faces"):
        asset_id = asset_id or _ingest(env, f"retired/{uuid.uuid4().hex[:8]}.jpg", media_type="image")
        body = {"detection_model": "apple_vision", "detection_model_version": "1", "faces": [FACE],
                "lineage": made("lumiverb.image.faces")}
    url = path.format(library_id=library_id, asset_id=asset_id)
    return client.request(method, url, json=body, params=params, headers=headers)


@pytest.mark.parametrize(("method", "path", "replaced_by", "says"), RETIRED, ids=IDS)
def test_an_editor_is_told_what_replaced_it(env, method, path, replaced_by, says):
    r = _as_an_old_client(env, method, path, _key(env, "editor"))
    assert r.status_code == 410, r.text
    err = r.json()["error"]
    assert err["code"] == "gone"
    assert all(words in err["message"] for words in says), err["message"]
    assert err["details"] == {"replaced_by": replaced_by}


def test_an_old_clients_upsert_makes_no_clip(env):
    client, headers, library_id, *_ = env
    assert _as_an_old_client(env, "POST", "/v1/assets/upsert", _key(env, "editor")).status_code == 410
    r = client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": "retired/a.jpg"},
                   headers=headers)
    assert r.status_code == 404, r.text


def test_an_old_clients_move_moves_nothing(env):
    client, headers, *_ = env
    clip = _ingest(env, "retired/stays.mov")
    r = client.post("/v1/assets/batch-moves", json={"items": [{"asset_id": clip, "rel_path": "retired/moved.mov"}]},
                    headers=_key(env, "editor"))
    assert r.status_code == 410, r.text
    assert client.get(f"/v1/assets/{clip}", headers=headers).json()["rel_path"] == "retired/stays.mov"


def test_an_old_clients_faces_save_nothing(env):
    client, headers, *_ = env
    photo = _ingest(env, "retired/faces.jpg", media_type="image")
    r = _as_an_old_client(env, "POST", "/v1/assets/{asset_id}/faces", _key(env, "editor"), asset_id=photo)
    assert r.status_code == 410, r.text
    assert client.get(f"/v1/assets/{photo}/faces", headers=headers).json() == {"faces": []}
    assert client.get(f"/v1/assets/{photo}", headers=headers).json()["face_count"] is None  # not looked for yet


@pytest.mark.parametrize(("method", "path", "replaced_by", "says"), RETIRED, ids=IDS)
def test_without_a_token_it_is_401_and_nothing_more(env, method, path, replaced_by, says):
    r = _as_an_old_client(env, method, path, None)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path", "replaced_by", "says"), RETIRED, ids=IDS)
def test_a_viewer_meets_the_role_check_first(env, method, path, replaced_by, says):
    # The writes were an editor's (403 for a viewer); the lists any signed-in role's.
    r = _as_an_old_client(env, method, path, _key(env, "viewer"))
    assert r.status_code == (410 if method == "GET" else 403), r.text


def _public_library(env) -> str:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": f"Public {uuid.uuid4().hex[:8]}", "root_path": "/retired-public",
                                           "is_public": True}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    assert client.get(f"/v1/libraries/{library_id}", params={"public_library_id": library_id}).status_code == 200
    return library_id


def test_a_public_pages_visitor_gets_401_for_the_list(env):
    client, *_ = env
    library_id = _public_library(env)
    r = client.get(f"/v1/libraries/{library_id}/ignored-paths", params={"public_library_id": library_id})
    assert r.status_code == 401, r.text


def test_a_public_librarys_visitor_is_told_the_page_is_gone(env):
    # A visitor could page a public library's clips: now they're told what replaced it, as anyone is.
    client, *_ = env
    library_id = _public_library(env)
    r = client.get("/v1/assets/page", params={"library_id": library_id})
    assert r.status_code == 410, r.text
    assert r.json()["error"]["details"] == {"replaced_by": ["GET /v1/query"]}
    assert client.get("/v1/query", params={"f": f"library:{library_id}"}).status_code == 200
