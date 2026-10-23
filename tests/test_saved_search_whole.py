# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A search saved as a project is saved whole, in its order, or refused.

Review of PR 10: a text search was cut to its first 1,000 matches without a
word, and a filter the server couldn't read was dropped, widening the
project to the whole library.

Text searches here are the real one (src/server/search/text_search.py) over
real clips: each test's own word, in its clips' paths (written by the
search sync) or their scenes' descriptions.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from tests.scenes import Scene, described_scenes
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow


def _clone(tenant_url: str, src_id: str, n: int, prefix: str) -> list[str]:
    """n copies of an asset, each at its own path and a minute earlier than the last."""
    eng = create_engine(tenant_url)
    with eng.begin() as c:
        cols = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='assets' ORDER BY ordinal_position"))]
        sel = []
        for col in cols:
            if col == "asset_id":
                sel.append(f"'ast_{prefix}_' || lpad(g::text, 5, '0')")
            elif col == "rel_path":
                sel.append(f"'{prefix}/' || lpad(g::text, 5, '0') || '.jpg'")
            elif col == "taken_at":
                sel.append("TIMESTAMPTZ '2024-01-01' - g * INTERVAL '1 minute'")
            else:
                sel.append(col)
        c.execute(text(f"INSERT INTO assets ({', '.join(cols)}) SELECT {', '.join(sel)} FROM assets,"
                       f" generate_series(1, :n) g WHERE asset_id = :src"), {"n": n, "src": src_id})
    eng.dispose()
    return [f"ast_{prefix}_{i:05d}" for i in range(1, n + 1)]


def _project_ids(client, headers, project_id) -> list[str]:
    ids, after = [], None
    while True:
        params = {"limit": 500, **({"after": after} if after else {})}
        r = client.get(f"/v1/projects/{project_id}/assets", params=params, headers=headers)
        assert r.status_code == 200, r.text
        ids += [i["asset_id"] for i in r.json()["items"]]
        after = r.json()["next_cursor"]
        if not after:
            return ids


def _synced(env) -> None:
    """The upkeep's search sync, until it has written every stale clip
    (one sweep goes on for so long: a sweep that writes nothing is the last)."""
    client, headers, *_ = env
    for _ in range(10):
        r = client.post("/v1/upkeep/search-sync", headers=headers)
        assert r.status_code == 200 and r.json()["failed"] == 0, r.text
        if r.json()["synced"] == 0:
            return
    raise AssertionError("the search sync never caught up")


def _save(env, name: str, word: str):
    client, headers, library_id, *_ = env
    return client.post("/v1/projects", json={"name": name, "from_search": {"filters": [
        {"type": "query", "value": word}, {"type": "library", "value": library_id}]}}, headers=headers)


def _projects_named(client, headers, name: str) -> int:
    return sum(p["name"] == name for p in client.get("/v1/projects", headers=headers).json()["items"])


def test_a_text_search_is_saved_whole(env) -> None:
    _client, _headers, _library_id, _storage, _tid, tenant_url = env
    base = _ingest(env, "seashore/base.jpg", media_type="image")
    _clone(tenant_url, base, 1499, "seashore")  # 1,500 with the base: past one page of 1,000
    _synced(env)
    r = _save(env, "Seashore", "seashore")
    assert r.status_code == 201, r.text
    assert r.json()["asset_count"] == 1500


def test_a_text_search_whose_matches_were_capped_is_refused(env) -> None:
    # The candidate set stops at its cap: past it, some matches would be left out unseen.
    client, headers, _library_id, _storage, _tid, tenant_url = env
    base = _ingest(env, "lagoon/base.jpg", media_type="image")
    _clone(tenant_url, base, 49, "lagoon")
    _synced(env)
    with patch("src.server.api.routers.query.MAX_CANDIDATE_IDS", 50):
        r = _save(env, "Capped", "lagoon")
    assert r.status_code == 422 and r.json()["error"]["code"] == "search_too_big", r.text
    assert r.json()["error"]["details"] == {"max": 50}
    assert _projects_named(client, headers, "Capped") == 0
    with patch("src.server.api.routers.query.MAX_CANDIDATE_IDS", 51):  # one more: it came back short
        r = _save(env, "Under the cap", "lagoon")
    assert r.status_code == 201 and r.json()["asset_count"] == 50, r.text


def test_a_full_scene_search_is_refused_though_few_clips_match(env) -> None:
    # Review round 2: each search is capped on its own and counts scenes,
    # not clips. 50 scenes over 10 videos filled the scene search: more
    # scenes (other videos) could have been left out, so it's refused.
    client, headers, *_ = env
    for v in range(10):
        video = _ingest(env, f"scn/{v}.mp4", media_type="video")
        described_scenes(client, headers, video, [
            Scene(s * 5000, s * 5000 + 4000, f"A dog playing, take {s}") for s in range(5)])
    with patch("src.server.api.routers.query.MAX_CANDIDATE_IDS", 50):
        r = _save(env, "Scenes", "dog")
    assert r.status_code == 422 and r.json()["error"]["code"] == "search_too_big", r.text
    assert _projects_named(client, headers, "Scenes") == 0
    with patch("src.server.api.routers.query.MAX_CANDIDATE_IDS", 51):  # one more: the 10 videos, whole
        r = _save(env, "Scenes under the cap", "dog")
    assert r.status_code == 201 and r.json()["asset_count"] == 10, r.text


def test_searches_that_came_back_short_are_saved_whole_however_many_together(env) -> None:
    # 30 clips found by their own words and 25 other videos by a scene: 55
    # clips past a cap of 50, but neither search was cut, so nothing was
    # left out.
    client, headers, _library_id, _storage, _tid, tenant_url = env
    base = _ingest(env, "reef/base.jpg", media_type="image")
    _clone(tenant_url, base, 29, "reef")
    _synced(env)
    for v in range(25):
        video = _ingest(env, f"corals/{v}.mp4", media_type="video")
        described_scenes(client, headers, video, [Scene(0, 4000, "Fish over a reef at low tide")])
    with patch("src.server.api.routers.query.MAX_CANDIDATE_IDS", 50):
        r = _save(env, "Both", "reef")
    assert r.status_code == 201, r.text
    assert r.json()["asset_count"] == 55


def test_a_search_is_saved_in_the_order_it_shows_up_to_the_cap(env) -> None:
    client, headers, library_id, _storage, _tid, tenant_url = env
    base = _ingest(env, "many/base.jpg", media_type="image")
    _clone(tenant_url, base, 1199, "many")  # 1,200 with the base: past one page of 1,000
    search = {"filters": [{"type": "path", "value": "many"}, {"type": "library", "value": library_id}],
              "sort": "taken_at", "direction": "desc"}

    seen, after = [], None  # what the creator sees, paging GET /v1/query
    while True:
        params = [("f", "path:many"), ("f", f"library:{library_id}"), ("sort", "taken_at"), ("dir", "desc"),
                  ("limit", "500")] + ([("after", after)] if after else [])
        r = client.get("/v1/query", params=params, headers=headers)
        seen += [i["asset_id"] for i in r.json()["items"]]
        after = r.json().get("next_cursor")
        if not after:
            break
    assert len(seen) == 1200

    with patch("src.server.api.routers.projects._MAX_NEW_CLIPS", 1200):
        r = client.post("/v1/projects", json={"name": "Many", "from_search": search}, headers=headers)
    assert r.status_code == 201, r.text
    assert _project_ids(client, headers, r.json()["project_id"]) == seen

    with patch("src.server.api.routers.projects._MAX_NEW_CLIPS", 1199):
        r = client.post("/v1/projects", json={"name": "Many-1", "from_search": search}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "search_too_big", r.text


@pytest.mark.parametrize("search", [
    {"filters": [{"type": "camera", "value": "Canon"}]},  # a typo for camera_make: the whole library
    {"filters": [{"type": "media", "value": 5}]},
    {"filters": [], "direction": None},
    {"filters": [], "sort": "colour"},
    {"filters": "camera_make:Canon"},
    {"filter": [{"type": "camera_make", "value": "NoSuchCamera"}]},  # a typo for filters: everything
    {"filters": [{"type": "camera_make", "value": 5}]},
    {"filters": [{"type": "query", "value": ["beach"]}]},
    {"filters": [{"type": "tag", "value": {"a": 1}}]},
    {"filters": [{"type": "camera_make", "value": "Canon", "negate": True}]},
    {"filters": [{"type": "path", "value": "a\x00b"}]},
])
def test_a_search_it_cant_read_is_refused(env, search) -> None:
    client, headers, *_ = env
    _ingest(env, "wide/a.jpg", media_type="image")
    r = client.post("/v1/projects", json={"name": "Unreadable", "from_search": search}, headers=headers)
    # A NUL is refused before the route reads the search, as in anything a request sends (src/server/api/no_nul.py).
    refused = (400, "not_text") if "\\u0000" in json.dumps(search) else (422, "bad_search")
    assert (r.status_code, r.json()["error"]["code"]) == refused, r.text
    assert _projects_named(client, headers, "Unreadable") == 0


def test_a_trashed_clip_leaves_no_project_behind(env) -> None:
    client, headers, *_ = env
    kept = _ingest(env, "gone/a.jpg", media_type="image")
    gone = _ingest(env, "gone/b.jpg", media_type="image")
    client.request("DELETE", "/v1/assets", json={"asset_ids": [gone], "reason": "user"}, headers=headers)
    r = client.post("/v1/projects", json={"name": "Half gone", "asset_ids": [kept, gone]}, headers=headers)
    assert r.status_code == 404, r.text
    assert _projects_named(client, headers, "Half gone") == 0
