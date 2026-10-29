"""Unit tests for what a person typed into search, parsed (src/server/search
/query_builder.py parse_query). Pure functions, no DB, runs under the `fast`
marker. What the words then find is tests/test_text_search.py's."""

from __future__ import annotations

import pytest

from src.server.search.query_builder import QueryTerm, parse_query, pieces


def _words(raw: str) -> list[str]:
    return [t.text for t in parse_query(raw)]


@pytest.mark.fast
class TestWords:
    def test_lowercases(self) -> None:
        assert _words("Greeting Card") == ["greeting", "card"]

    def test_strips_punctuation(self) -> None:
        assert _words("hello, world!") == ["hello", "world"]

    def test_keeps_internal_hyphen(self) -> None:
        # "co-op" survives as one word.
        assert _words("co-op meeting") == ["co-op", "meeting"]

    def test_drops_query_syntax(self) -> None:
        # `:`, `^`, `&`, `|`, `!`, parens, `*`: nothing typed reaches the query as its syntax.
        assert _words('field:"phrase" (boost^5) a&b|c !d e*') == [
            "field", "phrase", "boost", "5", "a", "b", "c", "d", "e"]

    def test_empty(self) -> None:
        assert parse_query("") == []
        assert parse_query("   ") == []
        assert parse_query("!!!") == []
        assert parse_query("%_") == []


@pytest.mark.fast
class TestParseQuery:
    def test_empty(self) -> None:
        assert parse_query("") == []
        assert parse_query("   ") == []

    def test_free_tokens(self) -> None:
        terms = parse_query("Negative Space")
        assert len(terms) == 2
        assert all(not t.is_phrase for t in terms)
        assert [t.text for t in terms] == ["negative", "space"]

    def test_quoted_phrase(self) -> None:
        terms = parse_query('"negative space"')
        assert len(terms) == 1
        assert terms[0].is_phrase
        assert terms[0].text == "negative space"
        assert terms[0].tokens == ("negative", "space")

    def test_mixed_phrase_and_tokens(self) -> None:
        terms = parse_query('"negative space" beach sunset')
        assert len(terms) == 3
        assert terms[0].is_phrase and terms[0].text == "negative space"
        assert not terms[1].is_phrase and terms[1].text == "beach"
        assert not terms[2].is_phrase and terms[2].text == "sunset"

    def test_single_token_phrase_collapses_to_token(self) -> None:
        # A one-word phrase is identical to a free token match, so it
        # should not be marked is_phrase=True.
        terms = parse_query('"beach"')
        assert len(terms) == 1
        assert not terms[0].is_phrase
        assert terms[0].text == "beach"

    def test_unmatched_trailing_quote(self) -> None:
        # An unmatched trailing `"` falls through as a literal — the
        # regex only matches balanced pairs, so content after the last
        # unmatched quote is parsed as free tokens.
        terms = parse_query('beach "sunset')
        assert [t.text for t in terms] == ["beach", "sunset"]
        assert all(not t.is_phrase for t in terms)

    def test_a_quoted_phrases_syntax_is_dropped_too(self) -> None:
        # Phrase words are kept to the same characters as free ones.
        terms = parse_query('"negative: (space)"')
        assert terms[0].tokens == ("negative", "space") and terms[0].is_phrase


@pytest.mark.fast
class TestJoinedWords:
    """Words typed joined by `_` or `.`, a file's name: one thing, a phrase whether quoted or not (Robert, Oct 29:
    `DJI_20261025123119_0003_D` found its file and everything with a "d" in it)."""

    def test_a_file_name_is_a_phrase_of_its_words_and_as_typed(self) -> None:
        assert parse_query("DJI_20261025123119_0003_D.MP4") == [QueryTerm(
            ("dji", "20261025123119", "0003", "d", "mp4"), True, "dji_20261025123119_0003_d.mp4", joined=True)]
        assert parse_query("IMG_7616.JPG beach") == [
            QueryTerm(("img", "7616", "jpg"), True, "img_7616.jpg", joined=True), QueryTerm(("beach",), False)]

    def test_split_at_hyphens_too_as_a_path_is(self) -> None:
        assert parse_query("DJI_2026-10-25")[0].tokens == ("dji", "2026", "10", "25")

    def test_quoted_is_a_phrase_as_it_was_without_the_prefix(self) -> None:
        assert parse_query('"IMG_7616.JPG"') == [QueryTerm(("img", "7616", "jpg"), True, "img_7616.jpg")]
        assert parse_query('"img_7616.jpg sunset"')[0].typed == "img_7616.jpg sunset"
        assert parse_query('"negative space"')[0].typed is None  # nothing joined: its words are as typed

    def test_a_hyphenated_word_alone_is_still_a_word(self) -> None:
        assert parse_query("co-op 2026-10-25") == [QueryTerm(("co-op",), False), QueryTerm(("2026-10-25",), False)]
        assert parse_query('"co-op meeting"') == [QueryTerm(("co", "op", "meeting"), True, "co-op meeting")]

    def test_a_dot_or_underscore_ending_a_word_joins_nothing(self) -> None:
        assert _words("the end. next_ word") == ["the", "end", "next", "word"]
        assert parse_query("U.S.A.") == [QueryTerm(("u", "s", "a"), True, "u.s.a", joined=True)]

    def test_pieces_take_it_as_quoted_and_write_it_back_as_typed(self) -> None:
        typed = pieces('Sue IMG_7616.JPG "Disney castle"')
        assert [(p.words, p.quoted) for p in typed] == [
            (("sue",), False), (("img_7616.jpg",), True), (("disney", "castle"), True)]
        said = " ".join(p.said for p in typed)
        assert said == 'sue img_7616.jpg "disney castle"'
        assert parse_query(said) == parse_query('Sue IMG_7616.JPG "Disney castle"')  # written back, it reads the same
