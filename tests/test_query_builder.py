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

    def test_single_token_targets_priority_fields(self) -> None:
        q = build_quickwit_query("card")
        # All priority fields are queried explicitly. No boost syntax —
        # Quickwit's REST parser doesn't reliably support `^N`.
        assert "description:card" in q
        assert "tags:card" in q
        assert "note:card" in q
        assert "ocr_text:card" in q
        assert "transcript_text:card" in q
        assert "path_tokens:card" in q
        # Single-token query has no phrase clause
        assert '"' not in q
        # Definitely no boost syntax leaking in
        assert "^" not in q

    def test_prefix_clauses_added_for_long_tokens(self) -> None:
        # Tokens of length ≥ 3 get prefix matches so "disney" finds
        # "disneyland" via Quickwit's wildcard syntax. path_tokens is
        # excluded because directory-component prefixes are noisy.
        q = build_quickwit_query("disney")
        assert "description:disney*" in q
        assert "tags:disney*" in q
        assert "note:disney*" in q
        # path_tokens deliberately NOT prefixed
        assert "path_tokens:disney*" not in q
        # Exact whole-word match still present — BM25 will score it
        # higher than the prefix variant because exact tokens have
        # higher IDF (rarer than the prefix term family).
        assert "description:disney" in q

    def test_short_tokens_skip_prefix(self) -> None:
        # 1-2 character tokens are too noisy for prefix matches —
        # `a*` would match almost everything.
        q = build_quickwit_query("ab")
        assert "description:ab" in q  # exact still works
        assert "ab*" not in q  # no prefix variant

    def test_multi_token_phrase_clauses(self) -> None:
        q = build_quickwit_query("greeting card")
        # Phrase clauses on the high-signal fields
        assert 'description:"greeting card"' in q
        assert 'tags:"greeting card"' in q
        assert 'note:"greeting card"' in q
        # Per-token clauses still present
        assert "description:greeting" in q
        assert "description:card" in q
        # No boost syntax
        assert "^" not in q

    def test_phrase_clauses_listed_before_per_token(self) -> None:
        """Phrase clauses come first in the OR'd query — Quickwit's
        BM25 will naturally rank phrase matches higher than per-token
        because phrases are rarer (higher IDF), no boost needed."""
        q = build_quickwit_query("greeting card")
        assert q.index('description:"greeting card"') < q.index("description:greeting")

    def test_input_sanitization(self) -> None:
        # User can't inject Quickwit syntax via the query string
        q = build_quickwit_query('description:"injected" OR (foo)^99')
        # The literal words survive as tokens; the syntax is stripped
        assert "description:description" in q  # the word "description" matches itself as a token
        assert "description:injected" in q
        # No double-quoted phrase from user content
        assert '"injected"' not in q
        assert "^99" not in q

    def test_uppercase_normalized(self) -> None:
        q = build_quickwit_query("CARD")
        assert "description:card" in q
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
