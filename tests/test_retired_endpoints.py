# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The old scan protocol's endpoints answer 410 gone (ADR-018 decision 12).

Scans compare on the server now: a scan session decides what's new,
changed, moved and skipped (POST /v1/libraries/{id}/scans, routers/scans.py),
and a file becomes a clip when it's ingested. The routes a client used to
compare on its own stay defined, behind the role each always needed, and
answer 410 in the error envelope, naming what replaced them.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow

SCANS = "POST /v1/libraries/{library_id}/scans"
# (method, path, what replaced it)
RETIRED = [
    ("POST", "/v1/assets/upsert", [SCANS, "POST /v1/ingest"]),
    ("POST", "/v1/assets/batch-moves", [SCANS]),
    ("GET", "/v1/libraries/{library_id}/ignored-paths", [SCANS]),
]
IDS = [f"{method} {path}" for method, path, _ in RETIRED]


def _key(env, role: str) -> dict:
    client, headers, *_ = env
    r = client.post("/v1/keys", json={"label": f"retired-{role}", "role": role}, headers=headers)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['plaintext']}"}


def _as_an_old_client(env, method: str, path: str, headers: dict | None):
    """The request an old client sent: an upsert's file, a scan's move, or the list."""
    client, _, library_id, *_ = env
    body = None
    if path.endswith("/upsert"):
        body = {"library_id": library_id, "rel_path": "retired/a.jpg", "file_size": 1, "file_mtime": None,
                "media_type": "image"}
    elif path.endswith("/batch-moves"):
        body = {"items": [{"asset_id": _ingest(env, f"retired/{uuid.uuid4().hex[:8]}.mov"),
                           "rel_path": "retired/moved.mov"}]}
    return client.request(method, path.format(library_id=library_id), json=body, headers=headers)


@pytest.mark.parametrize(("method", "path", "replaced_by"), RETIRED, ids=IDS)
def test_an_editor_is_told_what_replaced_it(env, method, path, replaced_by):
    r = _as_an_old_client(env, method, path, _key(env, "editor"))
    assert r.status_code == 410, r.text
    err = r.json()["error"]
    assert err["code"] == "gone"
    assert "retired" in err["message"] and "scan session" in err["message"] and "ADR-018" in err["message"]
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


@pytest.mark.parametrize(("method", "path", "replaced_by"), RETIRED, ids=IDS)
def test_without_a_token_it_is_401_and_nothing_more(env, method, path, replaced_by):
    r = _as_an_old_client(env, method, path, None)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path", "replaced_by"), RETIRED, ids=IDS)
def test_a_viewer_meets_the_role_check_first(env, method, path, replaced_by):
    # The writes were an editor's (403 for a viewer); the list any signed-in role's.
    r = _as_an_old_client(env, method, path, _key(env, "viewer"))
    assert r.status_code == (410 if method == "GET" else 403), r.text


def test_a_public_pages_visitor_gets_401_for_the_list(env):
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": f"Public {uuid.uuid4().hex[:8]}", "root_path": "/retired-public",
                                           "is_public": True}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    assert client.get(f"/v1/libraries/{library_id}", params={"public_library_id": library_id}).status_code == 200
    r = client.get(f"/v1/libraries/{library_id}/ignored-paths", params={"public_library_id": library_id})
    assert r.status_code == 401, r.text
