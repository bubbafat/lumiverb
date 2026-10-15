"""Unit tests for what a person typed into search, parsed (src/server/search
/query_builder.py parse_query). Pure functions, no DB, runs under the `fast`
marker. What the words then find is tests/test_text_search.py's."""

from __future__ import annotations

import pytest

from src.server.search.query_builder import parse_query


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
