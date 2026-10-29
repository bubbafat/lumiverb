# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A search's best matches apart, the rest by date (GET /v1/query best=N; Robert, Oct 29: as Google Photos shows a
search, its most relevant first with a count, then everything it found by day). The best are those the search
names or that match all of it (src/server/search/rank.py `best`); without best=N a search is in relevance order,
as ever. The real API and database."""

from __future__ import annotations

from urllib.parse import quote

import pytest

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_search_ranking import _taken
from tests.test_text_search import _clip, _describe, _library, _sweep

pytestmark = pytest.mark.slow


def _query(env, lib: str, words: str, **params) -> dict:
    client, headers, *_ = env
    extra = "".join(f"&{k}={quote(str(v))}" for k, v in params.items() if k != "f") + "".join(
        f"&f={quote(f)}" for f in params.get("f", []))
    r = client.get(f"/v1/query?f=query:{quote(words)}&f=library:{lib}{extra}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(items: list[dict]) -> list[str]:
    return [i["asset_id"] for i in items]


@pytest.fixture
def coast(env):
    """Beaches over the years, two of them in Dundee, and a video of Dundee's beach."""
    lib = _library(env)
    clips = {}
    for name, media, said, when in (
            ("beach_2019", "image", "A sandy beach at low tide", "2019-06-01"),
            ("beach_2024", "image", "A beach with surfers", "2024-06-01"),
            ("dundee_beach_2021", "image", "Dundee's beach at sunset", "2021-06-01"),
            ("dundee_beach_2023", "video", "Children on Dundee's beach", "2023-06-01"),
            ("dundee_bridge_2022", "image", "A bridge at Dundee", "2022-06-01")):
        clips[name] = _clip(env, lib, f"Coast/{name}.{'mp4' if media == 'video' else 'jpg'}", media)
        _describe(env, clips[name], said)
        _taken(env, clips[name], when)
    _sweep(env)
    return lib, clips


def test_the_best_apart_by_relevance_and_everything_found_by_date(env, coast):
    lib, c = coast
    said = _query(env, lib, "dundee beach", best=10)
    # Matching all of it: Dundee's beach, newest first on a tie.
    assert _ids(said["best"]["items"]) == [c["dundee_beach_2023"], c["dundee_beach_2021"]]
    assert said["best"]["total"] == 2
    # Everything found, by date (the page's sort), the best among them.
    assert _ids(said["items"]) == [c["beach_2024"], c["dundee_beach_2023"], c["dundee_bridge_2022"],
                                   c["dundee_beach_2021"], c["beach_2019"]]


def test_without_best_a_search_is_in_relevance_order_as_ever(env, coast):
    lib, c = coast
    said = _query(env, lib, "dundee beach")
    assert said["best"] is None
    assert _ids(said["items"])[:2] == [c["dundee_beach_2023"], c["dundee_beach_2021"]]


def test_only_so_many_best_with_how_many_there_are(env, coast):
    lib, c = coast
    said = _query(env, lib, "beach", best=1)  # one word: everything found matches all of it
    assert len(said["best"]["items"]) == 1 and said["best"]["total"] == 4


def test_the_pages_filters_count_for_the_best_too(env, coast):
    lib, c = coast
    said = _query(env, lib, "dundee beach", best=10, f=["media:video"])
    assert _ids(said["best"]["items"]) == [c["dundee_beach_2023"]] and said["best"]["total"] == 1
    assert _ids(said["items"]) == [c["dundee_beach_2023"]]


def test_the_best_come_with_the_first_page_and_the_rest_page_by_date(env, coast):
    lib, c = coast
    first = _query(env, lib, "dundee beach", best=10, limit=2)
    assert _ids(first["items"]) == [c["beach_2024"], c["dundee_beach_2023"]] and first["next_cursor"]
    second = _query(env, lib, "dundee beach", best=10, limit=2, after=first["next_cursor"])
    assert second["best"] is None
    assert _ids(second["items"]) == [c["dundee_bridge_2022"], c["dundee_beach_2021"]]


def test_a_clip_the_search_names_is_among_the_best(env):
    lib = _library(env)
    named = _clip(env, lib, "Cruise/DJI_20261025123119_0003_D.MP4", "video")
    other = _clip(env, lib, "Cruise/DJI_20261025124500_0004_D.MP4", "video")
    _sweep(env)
    said = _query(env, lib, "DJI_20261025123119_0003_D", best=10)
    assert _ids(said["best"]["items"]) == [named] and said["best"]["total"] == 1
    assert _ids(said["items"]) == [named], "its words joined, one thing: not the other by its DJI and D"
    said = _query(env, lib, "DJI_20261025", best=10)  # the start of a name: that day's
    assert set(_ids(said["items"])) == {named, other}
