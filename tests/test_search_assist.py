"""What an AI-assisted search asks about and takes from the machine's reply (src/server/search/assist.py), and how the
things of a kind join a search's words (src/server/search/text_search.py build). End to end:
tests/test_search_assisted.py."""

from __future__ import annotations

import pytest

from src.server.search.assist import MOST_ALSO, PROMPT, asked_about, said
from src.server.search.text_search import build

pytestmark = pytest.mark.fast


def test_the_words_asked_about_are_the_ones_that_could_name_a_kind():
    assert asked_about('a video of snacks at the "food court" in 2026') == ["video", "snacks"]
    assert asked_about("snacks Snacks snäcks") == ["snacks"]  # each once, case and accents aside
    assert asked_about("ok go") == []  # too short to be asked about


def test_a_reply_is_its_things_cleaned():
    reply = 'Sure! {"also": ["Popcorn", "tortilla  chips", "snacks", "snack bar", "ice-cream", 7, "a b c", "popcorn"]}'
    assert said(reply, "snacks") == ["popcorn", "tortilla chips", "snack bar", "ice-cream"]
    assert said('{"also": []}', "castle") == []
    assert len(said('{"also": [%s]}' % ", ".join(f'"thing{chr(97 + i)}"' for i in range(20)), "x")) == MOST_ALSO


@pytest.mark.parametrize("reply", ["no JSON here", '{"also": "popcorn"}', '{"other": []}'])
def test_a_reply_that_isnt_the_list_asked_for_is_refused(reply):
    with pytest.raises(ValueError):
        said(reply, "snacks")


def test_the_prompt_asks_for_things_of_the_kind_alone():
    asked = PROMPT.format(word="snacks", most=MOST_ALSO)
    assert "'snacks'" in asked and '"also"' in asked and "popcorn but not pizza" in asked


def test_the_things_of_a_kind_match_as_the_words_do():
    q = build(["snacks"], [{"snacks": ["popcorn", "ice cream"]}])
    assert "snacks" in q.params.values() and "popcorn" in q.params.values() and "ice cream" in q.params.values()
    assert q.exact.count("phraseto_tsquery") == 1  # a thing of two words, as a phrase
    assert build(["snacks"]).exact == build(["snacks"], [{}]).exact  # nothing more: as it was
    assert len(q.words) == 1  # the things of a kind are its word's: matching one is matching it
