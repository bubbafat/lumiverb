# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A clip added to a project goes after the ones already in it.

A project's clips are in position order, and its cover, unless one was
chosen, is its first clip. When the only clip left was at position 0, the
next one added was given 0 as well, so which was first was up to Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow


def _positions(env, project_id: str) -> list[tuple[str, int]]:
    with _db(env) as s:
        rows = s.execute(text("SELECT asset_id, position FROM project_assets WHERE project_id = :p"
                              " ORDER BY position, asset_id"), {"p": project_id}).all()
    return [(r.asset_id, r.position) for r in rows]


def test_a_clip_added_after_one_at_position_zero_comes_after_it(env):
    client, headers, *_ = env
    lib = _library(env, "Positions")
    first, second, third = (_ingest_with(lib, f"{n}.jpg", _sha(), None) for n in "abc")
    r = client.post("/v1/projects", json={"name": "Positions", "asset_ids": [first]}, headers=headers)
    assert r.status_code == 201, r.text
    project_id = r.json()["project_id"]
    for clip in (second, third):
        r = client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [clip]}, headers=headers)
        assert r.status_code == 200, r.text
    assert _positions(env, project_id) == [(first, 0), (second, 1), (third, 2)]
