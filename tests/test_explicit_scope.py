# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Deleting for good says what it deletes: ids, or all: true.

A request that names neither (or both) is a 400 scope_required, never
"everything" by omission (Robert's rule: impactful actions never infer
their scope from a missing argument).
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow


@pytest.mark.parametrize("method,path,ids", [
    ("DELETE", "/v1/trash/empty", "asset_ids"),
    ("POST", "/v1/libraries/empty-trash", "library_ids"),
    ("POST", "/v1/projects/empty-trash", "project_ids"),
    ("DELETE", "/v1/archive/missing", "library_id"),
])
def test_no_target_is_a_400(env, method, path, ids):
    client, headers, *_ = env
    for body in ({}, {"remove_from_projects": True}):
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 400, (body, r.text)
        assert r.json()["error"]["code"] == "scope_required"
    both = {ids: "x" if ids == "library_id" else ["x"], "all": True}
    r = client.request(method, path, json=both, headers=headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "scope_required"


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/libraries/empty-trash"),
    ("POST", "/v1/projects/empty-trash"),
])
def test_no_body_is_refused_too(env, method, path):
    client, headers, *_ = env
    r = client.request(method, path, headers=headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_all_true_empties_the_trash(env):
    client, headers, *_ = env
    for method, path in (("DELETE", "/v1/trash/empty"), ("POST", "/v1/libraries/empty-trash"),
                         ("POST", "/v1/projects/empty-trash")):
        r = client.request(method, path, json={"all": True, "remove_from_projects": True}, headers=headers)
        assert r.status_code == 200, (path, r.text)
        assert "deleted" in r.json()
