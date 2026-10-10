# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Marking most of a library missing asks first (409 mass_missing).

A half-mounted volume looks exactly like files gone: more than 50 clips and
more than half of a library's clips in sight waits for the count back.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture


def _library_of(env, n: int) -> list[str]:
    client, headers, *_ = env
    name = f"mass-{uuid.uuid4().hex[:8]}"
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    for i in range(n):
        r = client.post("/v1/assets/upsert", json={"library_id": library_id, "rel_path": f"f{i}.jpg",
                                                   "file_size": 1, "file_mtime": None, "media_type": "image"},
                        headers=headers)
        assert r.status_code == 200, r.text
    r = client.get("/v1/assets", params={"library_id": library_id}, headers=headers)
    return sorted(a["asset_id"] for a in r.json())


def _missing(env, ids: list[str], **extra):
    client, headers, *_ = env
    return client.request("DELETE", "/v1/assets", json={"asset_ids": ids, "reason": "missing", **extra},
                          headers=headers)


@pytest.mark.slow
def test_most_of_a_library_missing_asks_first(env):
    ids = _library_of(env, 100)
    r = _missing(env, ids[:51])  # 51 clips, 51%
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "mass_missing" and err["details"]["count"] == 51
    assert err["details"]["libraries"][0]["in_sight"] == 100
    client, headers, *_ = env
    assert len(client.get("/v1/assets", params={"library_id": err["details"]["libraries"][0]["library_id"]},
                          headers=headers).json()) == 100  # nothing taken

    assert _missing(env, ids[:51], confirm_missing=50).status_code == 409  # another count asks again
    r = _missing(env, ids[:51], confirm_missing=51)
    assert r.status_code == 200, r.text
    assert len(r.json()["trashed"]) == 51


@pytest.mark.slow
@pytest.mark.parametrize(("library", "going"), [(100, 50), (100, 49), (200, 60)])
def test_up_to_fifty_or_up_to_half_goes_ahead(env, library, going):
    ids = _library_of(env, library)
    r = _missing(env, ids[:going])
    assert r.status_code == 200, r.text
    assert len(r.json()["trashed"]) == going


@pytest.mark.slow
def test_a_persons_trash_isnt_held_by_the_rule(env):
    ids = _library_of(env, 60)
    client, headers, *_ = env
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": ids, "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
