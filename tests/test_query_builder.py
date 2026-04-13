"""Unit tests for the search query builder. Pure functions, no DB,
runs under the `fast` marker."""

from __future__ import annotations

import pytest

from src.server.search.query_builder import (
    build_quickwit_query,
    postgres_rank_clauses,
    tokenize,
)


@pytest.mark.fast
class TestTokenize:
    def test_lowercases(self) -> None:
        assert tokenize("Greeting Card") == ["greeting", "card"]

    def test_strips_punctuation(self) -> None:
        assert tokenize("hello, world!") == ["hello", "world"]

    def test_keeps_internal_hyphen(self) -> None:
        # "co-op" should survive as one token; default Quickwit
        # tokenizer also keeps hyphenated words intact.
        assert tokenize("co-op meeting") == ["co-op", "meeting"]

    def test_drops_quickwit_reserved_chars(self) -> None:
        # `:`, `^`, parens, quotes — anything the Quickwit query parser
        # treats as syntax must not survive into the query string.
        assert tokenize('field:"phrase" (boost^5)') == ["field", "phrase", "boost", "5"]

    def test_empty(self) -> None:
        assert tokenize("") == []
        assert tokenize("   ") == []
        assert tokenize("!!!") == []


@pytest.mark.fast
class TestBuildQuickwitQuery:
    def test_empty_returns_empty(self) -> None:
        assert build_quickwit_query("") == ""
        assert build_quickwit_query("   ") == ""
        assert build_quickwit_query("???") == ""

    def test_single_token_field_boosts(self) -> None:
        q = build_quickwit_query("card")
        # Description and tags should be present with the highest boost
        assert "description:card^5" in q
        assert "tags:card^5" in q
        # Note has medium boost
        assert "note:card^3" in q
        # OCR / transcript / path get lower boosts
        assert "ocr_text:card^2" in q
        assert "transcript_text:card^2" in q
        assert "path_tokens:card^1" in q
        # Single-token query has no phrase clause
        assert '"' not in q

    def test_multi_token_phrase_boost(self) -> None:
        q = build_quickwit_query("greeting card")
        # Phrase clauses come first, with the highest boost
        assert 'description:"greeting card"^15' in q
        assert 'tags:"greeting card"^15' in q
        assert 'note:"greeting card"^9' in q
        # Per-token clauses still present
        assert "description:greeting^5" in q
        assert "description:card^5" in q

    def test_phrase_clauses_outweigh_per_token(self) -> None:
        """The phrase clauses must rank ahead of per-token clauses so
        a literal "greeting card" beats a doc that just contains both
        words scattered."""
        q = build_quickwit_query("greeting card")
        # Find the boost for the phrase and the per-token clauses
        assert q.index('description:"greeting card"^15') < q.index("description:greeting^5")

    def test_input_sanitization(self) -> None:
        # User can't inject Quickwit syntax via the query string
        q = build_quickwit_query('description:"injected" OR (foo)^99')
        # No raw colons or boosts from the user input — only our own
        assert "description:description^5" in q  # the literal word survives as a token
        # No double-quoted phrase from user content
        assert '"injected"' not in q
        assert "^99" not in q

    def test_uppercase_normalized(self) -> None:
        q = build_quickwit_query("CARD")
        assert "description:card^5" in q
        assert "CARD" not in q


@pytest.mark.fast
class TestPostgresRankClauses:
    def test_empty_query(self) -> None:
        expr, params = postgres_rank_clauses("")
        assert expr == "0"
        assert params == {}

    def test_single_token_builds_token_pattern(self) -> None:
        expr, params = postgres_rank_clauses("card")
        # Single token: phrase pattern equals token pattern (one word)
        assert "rank_phrase" in params
        assert "rank_token" in params
        # Whole-word anchors must be present
        assert r"\m" in params["rank_phrase"]
        assert r"\M" in params["rank_phrase"]
        # Three-tier rank: phrase, token, fallback
        assert "WHEN" in expr and "THEN 0" in expr and "THEN 1" in expr and "ELSE 2" in expr

    def test_multi_token_phrase_pattern(self) -> None:
        _, params = postgres_rank_clauses("greeting card")
        # Phrase pattern should match the two tokens with whitespace between
        assert r"greeting\s+card" in params["rank_phrase"]
        # Token pattern is an alternation
        assert "greeting" in params["rank_token"]
        assert "card" in params["rank_token"]
        assert "|" in params["rank_token"]

    def test_regex_metacharacters_escaped(self) -> None:
        # Tokens shouldn't be able to inject regex syntax. Tokenize
        # already drops most regex metas, but underscores / hyphens
        # are allowed and should still be safe inside the regex.
        _, params = postgres_rank_clauses("co-op")
        # Hyphen is escaped to avoid range ambiguity inside char classes
        assert r"co\-op" in params["rank_phrase"] or "co-op" in params["rank_phrase"]
