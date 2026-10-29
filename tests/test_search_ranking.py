# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A search's clips in rank order, end to end (src/server/search/rank.py, GET /v1/query): a clip the search names
first; then by how much of the search it matches, a rare word weighing more; then where (a person's words, where
it was, the description, the text read, a path); newest first on a tie. The real database and search; only the
places plug-in is stood in for. The tiers' arithmetic: tests/test_search_rank.py."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_search_understood import (  # noqa: F401 — places: a fixture
    DISNEYLAND,
    PARIS,
    _at,
    places,
)
from tests.test_text_search import _clip, _db, _describe, _library, _note, _read, _search, _sweep

pytestmark = pytest.mark.slow


def _taken(env, clip: str, when: str) -> None:
    with _db(env) as s:
        s.execute(text("UPDATE assets SET taken_at = CAST(:t AS timestamp) WHERE asset_id = :a"), {"t": when, "a": clip})
        s.commit()


def test_a_file_named_by_the_search_comes_first(env):
    """Robert, Oct 29: DJI_20261025123119_0003_D found a thousand clips by its "D" and its "DJI" alone."""
    lib = _library(env)
    exact = _clip(env, lib, "Cruise/2026-10-25/DJI_20261025123119_0003_D.MP4", "video")
    other = _clip(env, lib, "Cruise/2026-10-25/DJI_20261025124500_0004_D.MP4", "video")
    angkor = _clip(env, lib, "Paris/DSCF1740.JPG")
    _describe(env, angkor, "A poster for 'Bronzes royaux d'Angkor' and 'Haikus d'argent' at a museum")
    osmo = _clip(env, lib, "Ship/IMG_0001.JPG")
    _describe(env, osmo, "A person holds a DJI Osmo Pocket camera on the deck")
    photo = _clip(env, lib, "Phone/IMG_7616.JPG")
    listed = _clip(env, lib, "Phone/IMG_0003.PNG")
    _read(env, listed, "Exported DJI_20261025123119_0003_D")
    _sweep(env)  # a clip's file name is searched once its search row is written

    # Its words joined, one thing (src/server/search/query_builder.py): the file it names first, then the
    # text read that says it; nothing by its "D" or "DJI" alone.
    assert _search(env, lib, "DJI_20261025123119_0003_D") == [exact, listed]
    found = _search(env, lib, "dji d")
    assert {exact, other, angkor, osmo} <= set(found), "typed apart, any of them as ever"
    assert _search(env, lib, "IMG_7616.JPG")[0] == photo  # with its extension
    assert _search(env, lib, "img_7616")[0] == photo  # without, any case

    # Named by the search beats words matched in a description, which count more than a file name's.
    beach = _clip(env, lib, "Shots/beach.jpg")
    described = _clip(env, lib, "Shots/IMG_0002.JPG")
    _describe(env, described, "A beach at low tide")
    _sweep(env)
    assert _search(env, lib, "beach") == [beach, described]


def test_a_rare_word_ranks_above_a_common_one(env):
    lib = _library(env)
    beaches = [_clip(env, lib, f"Trip/{n}.jpg") for n in range(3)]
    for clip in beaches:
        _describe(env, clip, "A sandy beach at low tide")
    dundee = _clip(env, lib, "Trip/d.jpg")
    _describe(env, dundee, "A bridge over the river at Dundee")
    both = _clip(env, lib, "Trip/both.jpg")
    _describe(env, both, "Dundee's beach at sunset")
    found = _search(env, lib, "dundee beach")
    assert found[:2] == [both, dundee] and set(found[2:]) == set(beaches)


def test_a_persons_words_rank_above_the_machines(env):
    client, headers, *_ = env
    lib = _library(env)
    machine = _clip(env, lib, "Day/a.jpg")
    _describe(env, machine, "A lighthouse on a rocky point")
    noted = _clip(env, lib, "Day/b.jpg")
    _describe(env, noted, "A harbour")
    _note(env, noted, "the lighthouse Dad painted")
    corrected = _clip(env, lib, "Day/c.jpg")
    _describe(env, corrected, "A harbour")
    r = client.patch(f"/v1/assets/{corrected}/corrections", json={"description": "Marblehead lighthouse at dawn"},
                     headers=headers)
    assert r.status_code == 200, r.text
    found = _search(env, lib, "lighthouse")
    assert set(found[:2]) == {noted, corrected} and found[2] == machine


def test_being_at_a_place_ranks_above_its_name_in_a_sign(env, places):
    lib = _library(env)
    there = _clip(env, lib, "Parks/carousel.jpg")
    _describe(env, there, "A carousel")
    _at(env, there, DISNEYLAND)
    sign = _clip(env, lib, "Paris/poster.jpg")
    _describe(env, sign, "A poster on a wall")
    _read(env, sign, "Come visit California Adventure")
    _at(env, sign, PARIS)
    _sweep(env)
    assert _search(env, lib, "California Adventure") == [there, sign]


def test_on_a_tie_the_newest_first(env):
    lib = _library(env)
    clips = {}
    for name, when in (("old", "2019-06-01"), ("new", "2025-06-01"), ("mid", "2022-06-01")):
        clips[name] = _clip(env, lib, f"Garden/{name}.jpg")
        _describe(env, clips[name], "Tulips in a garden")
        _taken(env, clips[name], when)
    assert _search(env, lib, "tulips") == [clips["new"], clips["mid"], clips["old"]]
