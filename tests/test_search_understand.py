"""What a search's words are understood to ask for (src/server/search/understand.py): who, where and what
kind, from the people and places it's handed. The search itself, through the API: tests/test_search_understood.py."""

from __future__ import annotations

import pytest

from src.server.search import nicknames
from src.server.search.understand import KIND, MOST_LOOKUPS, PERSON, PLACE, Person, Place, understand

pytestmark = pytest.mark.fast

PEOPLE = [Person("sue", "Susan Horvick"), Person("rob", "Robert Horvick"), Person("art", "Arthur Tuttle"),
          Person("aug", "August Tuttle"), Person("jon", "Jonathan Smith"), Person("jose", "José Núñez")]
DCA = Place("California Adventure", 33.806, -117.921)


class Places:
    """A place finder: these phrases name places the account's clips are around; it remembers what it was asked."""

    def __init__(self, **known: Place):
        self.known = {k.replace("_", " "): v for k, v in known.items()}
        self.asked: list[str] = []

    def __call__(self, phrase: str) -> Place | None:
        self.asked.append(phrase)
        return self.known.get(phrase)


def _parts(raw: str, places: Places | None = None) -> tuple[list[tuple[str, str, str]], str]:
    u = understand(raw, PEOPLE, places or Places())
    return [(p.kind, p.words, p.label) for p in u.parts], u.rest


def test_a_video_of_sue_dancing_at_california_adventure():
    parts, rest = _parts("a video of Sue dancing at California Adventure", Places(california_adventure=DCA))
    assert parts == [(KIND, "video", "videos"), (PERSON, "sue", "Susan Horvick"),
                     (PLACE, "california adventure", "California Adventure")]
    assert rest == "dancing"  # what's left to look for: the words that join others go
    u = understand("a video of Sue dancing at California Adventure", PEOPLE, Places(california_adventure=DCA))
    assert [p.person_ids for p in u.parts if p.kind == PERSON] == [("sue",)]
    assert [p.place for p in u.parts if p.kind == PLACE] == [DCA]
    assert [p.media for p in u.parts if p.kind == KIND] == ["video"]
    assert u.said()[-1] == {"kind": "words", "words": "dancing", "label": "dancing"}
    # In a clip's words a place is its name whole, a person any of their names.
    assert [p.searched for p in u.parts if p.kind == PLACE] == ['"california adventure"']
    assert "susan" in [p.searched for p in u.parts if p.kind == PERSON][0].split()


def test_a_name_is_its_nicknames_both_ways_but_not_a_name_of_its_own():
    assert _parts("Sue")[0] == [(PERSON, "sue", "Susan Horvick")]
    assert _parts("Bob")[0] == [(PERSON, "bob", "Robert Horvick")]
    assert "susan" in nicknames.same_as("sue") and "sue" in nicknames.same_as("susan")
    assert _parts("Nathan")[0] == []  # a name of its own, not Jonathan's
    assert _parts("jose")[0] == [(PERSON, "jose", "José Núñez")]  # accents aside


def test_both_names_in_order_name_one_person_and_a_last_name_a_family():
    assert _parts("Robert Horvick fireworks") == ([(PERSON, "robert horvick", "Robert Horvick")], "fireworks")
    assert _parts("Horvick")[0] == [(PERSON, "horvick", "Robert Horvick or Susan Horvick")]
    assert _parts("the Tuttles at the beach")[0] == [(PERSON, "tuttles", "Arthur Tuttle or August Tuttle")]
    # Two people: both asked for, each a part of its own.
    assert [p[2] for p in _parts("Sue and Robert")[0]] == ["Susan Horvick", "Robert Horvick"]


def test_words_that_join_others_are_never_a_name():
    people = [Person("will", "William Kay"), Person("don", "Don Lee")]
    assert understand("who will don the costume", people, Places()).parts == ()  # both stop words


def test_quoted_text_is_never_understood():
    parts, rest = _parts('"Sue" dancing', Places(dancing=DCA))
    assert parts == [(PLACE, "dancing", "California Adventure")] and rest == '"sue"'
    assert _parts('"Sue Horvick"')[0] == []


def test_words_typed_joined_are_taken_as_quoted_and_left_as_typed():
    # A file's name: one thing, never a name or a place, searched as it was typed (src/server/search
    # /query_builder.py: a phrase, its last word a prefix too, which quotes would lose).
    parts, rest = _parts("Sue IMG_7616.JPG", Places(img=DCA))
    assert parts == [(PERSON, "sue", "Susan Horvick")] and rest == "img_7616.jpg"
    assert _parts("sue_dancing.mov")[0] == []
    parts, rest = _parts('Sue "img_7616.jpg beach"')
    assert rest == '"img_7616.jpg beach"', "quoted text with a joined word, quoted again as typed"


def test_a_place_is_asked_for_after_at_or_in_first_then_the_longest_and_only_so_many():
    places = Places(california_adventure=DCA)
    assert _parts("dancing at California Adventure", places)[0] == [(PLACE, "california adventure",
                                                                      "California Adventure")]
    assert places.asked == ["california adventure"]  # after "at": asked first, and found
    places = Places()
    parts, rest = _parts("dog beach sunset", places)
    assert parts == [] and rest == "dog beach sunset"  # no place by any of those: words, as before
    assert places.asked == ["dog beach sunset", "dog beach", "beach sunset", "dog", "beach", "sunset"]
    places = Places()
    _parts("one two three four five six seven eight nine ten", places)
    assert len(places.asked) == MOST_LOOKUPS


def test_one_kind_is_asked_for_and_never_alone():
    assert _parts("photos of Sue")[0] == [(KIND, "photos", "photos"), (PERSON, "sue", "Susan Horvick")]
    assert [p[0] for p in _parts("photos and videos of Sue")[0]] == [PERSON]  # both kinds: neither
    u = understand("videos", PEOPLE, Places())
    assert (u.parts, u.rest) == ((), "videos")  # every video: the media filter's, the word searched as before


def test_a_search_with_nothing_understood_is_its_own_words():
    u = understand("a dog on the beach", PEOPLE, Places())
    assert u.parts == () and u.rest == "a dog on the beach"
