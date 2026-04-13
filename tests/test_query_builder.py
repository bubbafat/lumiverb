"""Unit tests for the search query builder. Pure functions, no DB,
runs under the `fast` marker."""

from __future__ import annotations

import pytest

from src.server.search.query_builder import (
    build_quickwit_prefix_query,
    build_quickwit_query,
    parse_query,
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

    def test_exact_query_has_no_prefix_clauses(self) -> None:
        # build_quickwit_query is now exact-only. Prefix expansion
        # lives in build_quickwit_prefix_query and is issued as a
        # separate Quickwit call so it can be position-scored
        # independently with a penalty.
        q = build_quickwit_query("disney")
        assert "description:disney" in q
        assert "*" not in q


@pytest.mark.fast
class TestBuildQuickwitPrefixQuery:
    def test_empty_returns_empty(self) -> None:
        assert build_quickwit_prefix_query("") == ""
        assert build_quickwit_prefix_query("???") == ""

    def test_single_token_emits_prefix_clauses(self) -> None:
        q = build_quickwit_prefix_query("disney")
        # Long-enough token gets prefix expansion across high-signal
        # fields. path_tokens is deliberately excluded — directory
        # component prefixes are noise.
        assert "description:disney*" in q
        assert "tags:disney*" in q
        assert "note:disney*" in q
        assert "path_tokens:disney*" not in q
        # No exact term clauses — that's the exact builder's job
        assert "description:disney " not in q + " "
        # No phrase clauses
        assert '"' not in q

    def test_short_tokens_skipped(self) -> None:
        # `a*` would match almost the entire corpus
        q = build_quickwit_prefix_query("a")
        assert q == ""
        q = build_quickwit_prefix_query("ab")
        assert q == ""

    def test_multi_token_each_token_prefixed(self) -> None:
        q = build_quickwit_prefix_query("disney parks")
        assert "description:disney*" in q
        assert "description:parks*" in q

    def test_uppercase_normalized(self) -> None:
        q = build_quickwit_prefix_query("DISNEY")
        assert "description:disney*" in q
        assert "DISNEY" not in q

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


@pytest.mark.fast
class TestQuickwitQueryQuotedPhrases:
    def test_quoted_phrase_produces_required_phrase_clause(self) -> None:
        q = build_quickwit_query('"negative space"')
        # Phrase clauses span ALL searchable fields (not just the
        # high-signal subset) because the user's explicit quoting
        # asks for exact match wherever the phrase appears.
        assert 'description:"negative space"' in q
        assert 'tags:"negative space"' in q
        assert 'note:"negative space"' in q
        assert 'ocr_text:"negative space"' in q
        assert 'transcript_text:"negative space"' in q
        assert 'path_tokens:"negative space"' in q
        # No per-token OR clauses — the phrase is exact, not loose
        assert "description:negative " not in q + " "
        assert "description:space " not in q + " "

    def test_quoted_phrase_has_no_prefix_expansion(self) -> None:
        q = build_quickwit_prefix_query('"negative space"')
        # Prefix expansion is skipped entirely for quoted phrases
        assert q == ""

    def test_mixed_phrase_and_token_anded(self) -> None:
        q = build_quickwit_query('"negative space" beach')
        # Required phrase group AND required free-token group
        assert " AND " in q
        assert 'description:"negative space"' in q
        assert "description:beach" in q

    def test_mixed_phrase_and_token_prefix_only_expands_free(self) -> None:
        q = build_quickwit_prefix_query('"negative space" beach')
        # beach is long enough to expand; negative/space are inside
        # the phrase so they're skipped
        assert "description:beach*" in q
        assert "negative" not in q
        assert "space" not in q

    def test_unquoted_matches_both_words(self) -> None:
        # Regression: the pre-quoting behavior for unquoted multi-word
        # queries must be preserved — flat OR across fields with an
        # automatic phrase clause on high-signal fields.
        q = build_quickwit_query("negative space")
        assert " AND " not in q
        assert 'description:"negative space"' in q
        assert "description:negative" in q
        assert "description:space" in q


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

    def test_quoted_phrase_ranks_phrase_pattern(self) -> None:
        _, params = postgres_rank_clauses('"negative space" beach')
        # Phrase pattern anchors on the quoted phrase, not the free
        # token. Token pattern still lists all words so "beach" alone
        # can still hit the mid-tier rank.
        assert r"negative\s+space" in params["rank_phrase"]
        assert "beach" not in params["rank_phrase"]
        assert "beach" in params["rank_token"]

    def test_regex_metacharacters_escaped(self) -> None:
        # Tokens shouldn't be able to inject regex syntax. Tokenize
        # already drops most regex metas, but underscores / hyphens
        # are allowed and should still be safe inside the regex.
        _, params = postgres_rank_clauses("co-op")
        # Hyphen is escaped to avoid range ambiguity inside char classes
        assert r"co\-op" in params["rank_phrase"] or "co-op" in params["rank_phrase"]
