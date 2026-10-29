# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What found each clip a search finds, end to end (GET /v1/query's search_context: matched_in, its snippet an
excerpt of what matched): a clip's description, note, the text read in it, its transcript, its folder or file name,
a scene, and for a search understood, where it was or who's in it. A plug-in's fact: tests/test_plugin_work.py.
The real database and search (src/server/search/text_search.py); only the places plug-in is stood in for."""

from __future__ import annotations

import pytest

from src.server.search import text_search
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_search_understood import (  # noqa: F401 — places: a fixture
    DISNEYLAND,
    _at,
    _face,
    _named,
    places,
)
from tests.test_text_search import (
    _clip,
    _db,
    _describe,
    _library,
    _note,
    _read,
    _scene,
    _search,
    _sweep,
    _transcribe,
)

pytestmark = pytest.mark.slow

SIGN = "AK\n0131 225 9824\nDundee Diner Kitchen\nENDS"  # what was read in a photo of a street sign


def _found_by(env, lib: str, *terms: str) -> dict[str, dict]:
    """Each clip found, and what found it."""
    return {i["asset_id"]: i["search_context"] for i in _search(env, lib, *terms, raw=True)["items"]}


@pytest.fixture
def dundee(env):
    """Clips that say Dundee each in one way."""
    lib = _library(env)
    clips = {name: _clip(env, lib, path, media) for name, path, media in (
        ("described", "Trip/diner.jpg", "image"), ("read", "Edinburgh/street.jpg", "image"),
        ("noted", "Trip/lunch.jpg", "image"), ("pathed", "Dundee/bridge.jpg", "image"),
        ("spoken", "Trip/drive.mp4", "video"), ("filmed", "Trip/walk.mp4", "video"))}
    _describe(env, clips["described"], "A diner sign in Dundee at night")
    _describe(env, clips["read"], "A street with shops")
    _read(env, clips["read"], SIGN)
    _describe(env, clips["noted"], "A plate of food")
    _note(env, clips["noted"], "Lunch on the way up to Dundee with everyone")
    _describe(env, clips["pathed"], "A bridge over a river")
    _transcribe(env, clips["spoken"], "1\n00:00:04,000 --> 00:00:06,000\nWe drove up to Dundee today\n")
    _scene(env, clips["filmed"], 12_000, 15_000, "A sign for Dundee by the road", ["sign"])
    _sweep(env)  # the text read in a picture is written to its row by the sweep
    return lib, clips


def test_each_clip_says_what_its_words_matched_in_with_an_excerpt_of_it(env, dundee):
    lib, clips = dundee
    found = _found_by(env, lib, "dundee")
    said = {name: (found[c]["matched_in"], found[c]["snippet"], found[c]["start_ms"]) for name, c in clips.items()}
    assert said == {
        "described": ("description", "A diner sign in Dundee at night", None),
        "read": ("text", "AK 0131 225 9824 Dundee Diner Kitchen ENDS", None),  # short: whole, on one line
        "noted": ("note", "Lunch on the way up to Dundee with everyone", None),
        "pathed": ("path", "A bridge over a river", None),  # its folder's name; its description, as ever
        "spoken": ("transcript", "We drove up to Dundee today", 4000),  # where it's said
        "filmed": ("scene", "A sign for Dundee by the road", 12_000),
    }
    assert {c["from_plugin"] for c in found.values()} == {None}


def test_a_long_text_is_cut_to_the_words_around_the_match(env):
    lib = _library(env)
    menu = _clip(env, lib, "Trip/menu.jpg")
    _describe(env, menu, "A menu on a wall")
    _read(env, menu, "Breakfast served all day. Eggs any style with toast and potatoes. Fresh fruit bowl with yogurt "
                     "and granola. Haggis, neeps and tatties, a Dundee favourite. Coffee, tea and fresh orange juice.")
    _sweep(env)
    said = _found_by(env, lib, "dundee")[menu]
    assert said["matched_in"] == "text" and "Dundee" in said["snippet"]
    assert not said["snippet"].startswith("Breakfast") and len(said["snippet"].split()) <= 14


def test_a_word_in_its_description_and_its_note_is_the_descriptions(env, dundee):
    lib, clips = dundee
    _note(env, clips["described"], "Dundee again")
    assert _found_by(env, lib, "dundee")[clips["described"]]["matched_in"] == "description"


def test_a_public_page_is_never_told_of_a_note_a_path_or_a_transcript_it_doesnt_play(env, dundee):
    """What a visitor's search reads (asset_search.shown, and shown_capped where only the start of a video
    plays) holds no note or path, nor a capped one the transcript: nothing it finds says one."""
    lib, clips = dundee
    q = text_search.build(["dundee"])
    with _db(env) as s:
        shown = {h.asset_id: h for h in text_search.clips(s, q, [lib], 50, public=True)}
        capped = {h.asset_id: h for h in text_search.clips(s, q, [lib], 50, public=True, public_cap_ms=1000)}
    assert {clips["noted"], clips["pathed"]}.isdisjoint(shown) and {clips["noted"], clips["pathed"]}.isdisjoint(capped)
    assert shown[clips["read"]].matched_in == capped[clips["read"]].matched_in == "text"
    assert shown[clips["spoken"]].matched_in == "transcript" and clips["spoken"] not in capped


def test_found_by_where_it_was_or_who_is_in_it_when_its_words_dont_say(env, places):
    lib = _library(env)
    there = _clip(env, lib, "Parks/a.jpg")
    _describe(env, there, "A carousel")
    _at(env, there, DISNEYLAND)
    named = _clip(env, lib, "Parks/b.jpg")
    _describe(env, named, "Fireworks over a castle at California Adventure")
    _at(env, named, DISNEYLAND)
    found = _found_by(env, lib, "California Adventure")
    assert (found[there]["matched_in"], found[there]["snippet"]) == ("place", "California Adventure")
    assert found[named]["matched_in"] == "description"  # its words say it: that first

    seen = _clip(env, lib, "Parks/c.jpg")
    _describe(env, seen, "A woman waving")
    _named(env, "Susan Horvick", [_face(env, seen)])
    found = _found_by(env, lib, "Sue")
    assert (found[seen]["matched_in"], found[seen]["snippet"]) == ("person", "Susan Horvick")
