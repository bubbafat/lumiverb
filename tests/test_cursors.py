# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A page's cursor is one its list gave, or a 400 bad_cursor (src/server/cursors.py).

A cursor's values were bound into each list's query as they came: a dict, text
where a time goes, or a NUL was the database's error, a 500, GET /v1/query's
from a public page too. Every list reads its cursor through cursors.py now:
the first tests read cursors directly; the rest send each list a cursor it
never gave, against the real API and database, and page through every sort
GET /v1/query offers with the cursors it does give. A NUL in a ?f= filter is
a 400 bad_filter.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.server import cursors
from tests.clips import make_clip
from tests.test_library_access import _key, _person
from tests.test_scans_api import _library, env  # noqa: F401 — the shared fixture


def _packed(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Reading one
# ---------------------------------------------------------------------------


def test_a_cursor_the_list_gave_reads_back() -> None:
    from src.server.api.routers.query import _encode_cursor

    at = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    read = cursors.unpacked(_encode_cursor("taken_at", at.isoformat(), "ast_1"), v=cursors.time, id=cursors.text)
    assert read == {"v": at, "id": "ast_1"}
    assert cursors.unpacked(_packed({"count": 3, "id": "per_1"}), count=cursors.whole, id=cursors.text) == {
        "count": 3, "id": "per_1"}
    assert cursors.unpacked(_packed({"v": None, "id": "x"}), v=cursors.or_none(cursors.number), id=cursors.text)["v"] is None
    assert cursors.time("2026-10-01T12:30:00Z") == at
    assert cursors.plain(None) is None and cursors.plain("ast_1") == "ast_1"


@pytest.mark.parametrize("cursor", [
    "not a cursor!", "%%%", _packed([1, 2]), _packed("ast_1"), _packed({"v": 1}), _packed({"v": 1, "id": "x", "more": 1}),
    _packed({"v": {"a": 1}, "id": "x"}), _packed({"v": [1], "id": "x"}), _packed({"v": 1, "id": {"a": 1}}),
    _packed({"v": 1, "id": "a\x00b"}), _packed({"v": 1, "id": "\ud800"}), _packed({"v": True, "id": "x"}),
    _packed({"v": 2**70, "id": "x"}), "a\x00b", "ünïcode",
])
def test_anything_else_is_a_bad_cursor(cursor) -> None:
    with pytest.raises(cursors.BadCursorError):
        cursors.unpacked(cursor, v=cursors.whole, id=cursors.text)


@pytest.mark.parametrize(("read", "value"), [
    (cursors.time, "not a time"), (cursors.time, 1700000000), (cursors.time, "2026-10-01\x00"),
    (cursors.number, float("inf")), (cursors.number, "1.5"), (cursors.whole, 1.5), (cursors.text, 7),
])
def test_each_value_is_of_its_kind(read, value) -> None:
    with pytest.raises(cursors.BadCursorError):
        read(value)


# ---------------------------------------------------------------------------
# Every list, against the API
# ---------------------------------------------------------------------------

BAD = ["not a cursor!", _packed([1]), _packed({"v": {"a": 1}, "id": "x"}), _packed({"v": "not a time", "id": "x"}),
       _packed({"v": None, "id": "a\x00b"}), _packed({"count": "many", "id": "x"}), "a\x00b"]


def _bad_cursor(r) -> None:
    assert r.status_code == 400, (r.status_code, r.text)
    assert r.json()["error"]["code"] == "bad_cursor", r.text


@pytest.fixture(scope="module")
def lists(env):
    """A library with a clip, a project holding it, and a person's key (favorites are a person's)."""
    library_id = _library(env)
    clip = make_clip(env.tenant_url, library_id, f"c/{uuid.uuid4().hex[:6]}.jpg")
    project = env.client.post("/v1/projects", json={"name": f"p-{uuid.uuid4().hex[:6]}"}, headers=env.admin)
    assert project.status_code in (200, 201), project.text
    project_id = project.json()["project_id"]
    assert env.client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [clip]},
                           headers=env.admin).status_code == 200
    person = _person(env, f"cursor-{uuid.uuid4().hex[:6]}@example.com", "editor")
    return library_id, project_id, _key(env, person, "editor")


@pytest.mark.slow
@pytest.mark.parametrize("cursor", BAD)
def test_every_list_answers_a_cursor_it_never_gave_with_400(env, lists, cursor) -> None:
    library_id, project_id, person = lists
    paths = [
        ("/v1/query", {"f": f"library:{library_id}"}, env.admin),
        ("/v1/query", {"f": "query:anything"}, env.admin),  # a text search's relevance order
        ("/v1/query", {"sort": "file_size"}, env.admin),
        ("/v1/people", {}, env.admin),
        ("/v1/people/dismissed", {}, env.admin),
        (f"/v1/projects/{project_id}/assets", {}, env.admin),
        ("/v1/archive", {}, env.admin),
        ("/v1/trash", {}, env.admin),
        ("/v1/producers/failures", {}, env.admin),
        ("/v1/assets/favorites", {}, person),
    ]
    for path, params, headers in paths:
        r = env.client.get(path, params={**params, "after": cursor}, headers=headers)
        if path == "/v1/query" and "query:" in params.get("f", "") and r.status_code == 200:
            assert r.json()["items"] == []  # nothing found: no page to place a cursor in
            continue
        _bad_cursor(r)


@pytest.mark.slow
@pytest.mark.parametrize("cursor", ["2026-10-01T00:00:00+00:00", "yesterday|ast_1/lumiverb.preview",
                                    "2026-10-01T00:00:00+00:00|ast_1\x00/x", ""])
def test_the_failures_list_reads_its_own_cursor_only(env, cursor) -> None:
    r = env.client.get("/v1/producers/failures", params={"after": cursor}, headers=env.admin)
    if cursor == "":
        assert r.status_code == 400  # not this list's cursor either (no time, no clip)
    _bad_cursor(r)


@pytest.mark.slow
def test_a_public_pages_search_with_a_bad_cursor_is_a_400(env) -> None:
    library_id = _library(env)
    make_clip(env.tenant_url, library_id, "p/one.jpg")
    assert env.client.patch(f"/v1/libraries/{library_id}", json={"is_public": True},
                            headers=env.admin).status_code == 200
    seen = env.client.get("/v1/query", params={"f": f"library:{library_id}"})
    assert seen.status_code == 200 and len(seen.json()["items"]) == 1, seen.text
    for cursor in BAD:
        _bad_cursor(env.client.get("/v1/query", params={"f": f"library:{library_id}", "after": cursor}))


@pytest.mark.slow
@pytest.mark.parametrize("sort", ["taken_at", "created_at", "changed_at", "file_size", "iso", "exposure_time_us",
                                  "aperture", "focal_length", "rel_path", "asset_id"])
@pytest.mark.parametrize("direction", ["desc", "asc"])
def test_the_cursors_it_gives_page_through_every_sort(env, sort, direction) -> None:
    library_id = _library(env)
    clips = [make_clip(env.tenant_url, library_id, f"s/{n}.jpg", file_size=1000 + n) for n in range(4)]
    base = datetime(2026, 1, 1, tzinfo=UTC)
    with env.db.begin() as conn:
        for n, clip in enumerate(clips[:3]):  # the last one has none of them: sorted last
            conn.execute(text("UPDATE assets SET taken_at = :t, iso = :iso, aperture = :a, focal_length = :f,"
                              " exposure_time_us = :e WHERE asset_id = :id"),
                         {"t": base + timedelta(days=n), "iso": 100 * (n + 1), "a": 1.4 + n, "f": 35.0 + n,
                          "e": 1000 * (n + 1), "id": clip})
    seen, after = [], None
    for _ in range(10):
        params = {"f": f"library:{library_id}", "sort": sort, "dir": direction, "limit": 1}
        r = env.client.get("/v1/query", params={**params, **({"after": after} if after else {})}, headers=env.admin)
        assert r.status_code == 200, r.text
        seen += [i["asset_id"] for i in r.json()["items"]]
        after = r.json()["next_cursor"]
        if not after:
            break
    assert sorted(seen) == sorted(clips) and len(seen) == len(clips), (sort, direction, seen)


@pytest.mark.slow
def test_a_nul_in_a_filter_is_a_400(env) -> None:
    for path in ("/v1/query", "/v1/assets/facets"):
        for f in ("camera_make:Can\x00on", "query:a\x00b", "library:lib_\x00"):
            r = env.client.get(path, params={"f": f}, headers=env.admin)
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_filter", (path, f, r.text)
        # Signed out, a library a public page can't be: no page, and never a 500.
        assert env.client.get(path, params={"f": "library:lib_\x00"}).status_code == 401
