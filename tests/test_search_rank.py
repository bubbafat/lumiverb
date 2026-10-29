"""How a search's clips are ranked (src/server/search/rank.py): the tiers in one score, and a rare word weighing
more. End to end: tests/test_search_ranking.py."""

from __future__ import annotations

import pytest

from src.server.search.rank import FIELD, FILE, FOLDER, Rank, coverage, typed_names

pytestmark = pytest.mark.fast


def test_each_tier_decides_before_the_next():
    named = Rank(named=FILE, covered=0.1, field=FIELD["path"], quality=0.1)
    folder = Rank(named=FOLDER, covered=1.0, field=FIELD["note"], quality=9.0)
    everything = Rank(covered=1.0, field=FIELD["path"], quality=0.1)
    most = Rank(covered=0.9, field=FIELD["note"], quality=9.0)
    noted = Rank(covered=0.9, field=FIELD["note"], quality=0.1)
    described = Rank(covered=0.9, field=FIELD["description"], quality=9.0)
    better = Rank(covered=0.9, field=FIELD["description"], quality=0.5)
    worse = Rank(covered=0.9, field=FIELD["description"], quality=0.4)
    ranked = [named, folder, everything, most, noted, described, better, worse]
    assert sorted(ranked, key=lambda r: -r.score)[:4] == [named, folder, everything, most]
    assert [r.score for r in ranked[4:]] == sorted((r.score for r in ranked[4:]), reverse=True)
    assert noted.score > described.score and better.score > worse.score


def test_a_rare_word_weighs_more_than_a_common_one():
    # "d" in three clips of four, the date in one: the clip with the date has most of the search.
    matched = {"file": [True, True], "a": [True, False], "b": [True, False], "c": [False, True]}
    covered = coverage(matched, 2)
    assert covered["file"] == 1.0
    assert covered["c"] > covered["a"] == covered["b"]


def test_a_word_that_matched_nothing_weighs_nothing():
    assert coverage({"a": [True, False], "b": [True, False]}, 2) == {"a": 1.0, "b": 1.0}
    assert coverage({"a": []}, 0) == {"a": 1.0}


def test_searches_combined_keep_their_shares_apart():
    both = Rank(covered=1.0, field=4).plus(Rank(covered=0.5, field=5))
    assert both.coverage == 0.75 and both.field == 9
    assert Rank(named=FOLDER).plus(Rank(named=FILE)).named == FILE


def test_what_a_search_could_name_a_clip_by():
    assert typed_names(['DJI_20261025123119_0003_D', '"IMG_7616.JPG"', "  Disney  Cruise 2026 ", "José"]) == [
        "dji_20261025123119_0003_d", "img_7616.jpg", "disney cruise 2026", "jose"]
