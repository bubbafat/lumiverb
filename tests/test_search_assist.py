"""What an AI-assisted search asks about and takes from the machine's reply (src/server/search/assist.py), and how the
things of a kind join a search's words (src/server/search/text_search.py build). End to end, with a real machine on
this computer and the real database: tests/test_search_assisted.py."""

from __future__ import annotations

import json

import pytest

from src.server.search.assist import MOST_ALSO, MOST_CHARS, PROMPT, asked_about, by_word, said
from src.server.search.text_search import build

pytestmark = pytest.mark.fast


def test_a_searchs_words_are_asked_about_together():
    assert asked_about('a video of snacks at the "food court" in 2026') == "video snacks 2026"
    assert asked_about("Disney Thème Park") == "disney theme park"  # in order, case and accents aside
    assert asked_about("ok go 2026") == ""  # none could name a kind: nothing asked
    assert asked_about("IMG_7616.JPG") == "" and asked_about("snacks IMG_7616.JPG") == "snacks"  # a file's name
    many = " ".join(["popcorn"] * (MOST_CHARS // 8 + 1))
    assert len(many) > MOST_CHARS and asked_about(many) == ""


def test_a_reply_is_its_kinds_each_some_of_the_words_in_a_row():
    reply = ('Sure! {"kinds": {"Theme  Park": ["Roller Coaster", "carousel", "water park", 7, "a b c", "carousel"], '
             '"park": ["swing"], "snacks": ["popcorn"], "pizza": ["calzone"], "disney": [], "sunset": "dusk"}}')
    found = said(reply, "snacks disney theme park")
    # "park" is part of "theme park", the longer; "pizza" isn't one of the words; "disney" lists nothing.
    assert list(found.items()) == [("snacks", ["popcorn"]), ("theme park", ["roller coaster", "carousel"])]
    assert said('{"kinds": {}}', "castle") == {}
    many = said(json.dumps({"kinds": {"x": [f"thing{chr(97 + i)}" for i in range(20)]}}), "x")
    assert len(many["x"]) == MOST_ALSO


@pytest.mark.parametrize("reply", ["no JSON here", '{"kinds": ["snacks"]}', '{"also": ["popcorn"]}'])
def test_a_reply_that_isnt_the_kinds_asked_for_is_refused(reply):
    with pytest.raises(ValueError):
        said(reply, "snacks")


def test_the_prompt_asks_about_the_words_together_and_for_things_of_a_kind_alone():
    asked = PROMPT.format(words="red car snacks", most=MOST_ALSO)
    assert "'red car snacks'" in asked and '"kinds"' in asked and "popcorn but not pizza" in asked
    assert "'car' is the kind, not 'red'" in asked


def test_a_kinds_things_match_as_its_last_word_does():
    also = by_word({"theme park": ["roller coaster", "carousel"], "red car": ["truck"], "snacks": ["popcorn"]})
    # A truck is a car, but not red: the search's "red" is still asked for.
    assert also == {"park": ["roller coaster", "carousel"], "car": ["truck"], "snacks": ["popcorn"]}
    q = build(["theme park"], [by_word({"theme park": ["roller coaster"]})])
    assert "roller coaster" in q.params.values() and q.exact.count("phraseto_tsquery") == 1  # a phrase
    assert len(q.words) == 2  # the things of a kind are its word's: matching one is matching it
    assert build(["snacks"]).exact == build(["snacks"], [{}]).exact  # nothing more: as it was
