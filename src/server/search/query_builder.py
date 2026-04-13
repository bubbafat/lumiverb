"""Query construction for Quickwit and Postgres text search.

The default Quickwit query (`combined_query`) just dumps the raw user
input into the engine, which then searches every default field with
equal weight. That made search underperform: an OCR'd street sign
saying "card" would outrank a photo whose AI description was literally
"Christmas card" because BM25 had no field-level signal to break the
tie. The schema's `default` tokenizer is already whole-word, so the
fix isn't about tokenization — it's about teaching the engine that a
match in `description`/`tags` is far more authoritative than a match
in `ocr_text`/`path_tokens`.

This module builds field-boosted Quickwit queries:

- Per-token, per-field clauses with boosts (description / tags > note >
  ocr_text / transcript_text > path_tokens).
- For multi-token queries, an additional **phrase boost** that fires
  when the entire phrase appears in description/tags/note. So
  "greeting card" finds the literal phrase before any per-word match.

It also exposes a small helper for ranking the Postgres ILIKE fallback
by whole-word presence so the fallback path doesn't undo the gains.
"""

from __future__ import annotations

import re

# Field boosts in descending authority. Numbers are arbitrary but the
# *ratio* matters: AI-derived signal beats extracted text by 2x or more,
# extracted text beats path tokens by 2x or more.
FIELD_BOOSTS: list[tuple[str, int]] = [
    ("description", 5),
    ("tags", 5),
    ("note", 3),
    ("transcript_text", 2),
    ("ocr_text", 2),
    ("path_tokens", 1),
]

# Fields where a phrase match is meaningful enough to warrant a big
# boost. We don't phrase-boost ocr_text or path_tokens because phrases
# in those fields are noisy / accidental.
PHRASE_BOOST_FIELDS: list[tuple[str, int]] = [
    ("description", 15),
    ("tags", 15),
    ("note", 9),
]

# Whitelist of characters allowed in a token. Quickwit's query parser is
# strict about reserved characters (`:`, `^`, `(`, `"` etc.), and the
# raw user input would inject syntax. Lowercase the input and keep only
# alphanumerics + hyphens — that matches what the `default` tokenizer
# stores in the index anyway.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def tokenize(query: str) -> list[str]:
    """Lowercase + extract tokens. Mirrors what Quickwit's `default`
    tokenizer would do at index time, so the tokens we send for matching
    line up with what's actually stored."""
    return _TOKEN_RE.findall(query.lower())


def build_quickwit_query(raw: str) -> str:
    """Build a field-boosted Quickwit query from raw user input.

    Returns an empty string when the input has no usable tokens — the
    caller should treat that as "no text search" and skip the engine
    entirely rather than sending a malformed query.
    """
    tokens = tokenize(raw)
    if not tokens:
        return ""

    parts: list[str] = []

    # 1. Phrase boost — multi-token queries get an exact-phrase clause
    # in description/tags/note with a much higher boost than per-token
    # clauses. This is what makes "greeting card" outrank "cardigan"
    # (which wouldn't even match) AND outrank a photo that just happens
    # to have "card" and "greeting" scattered across different fields.
    if len(tokens) > 1:
        phrase = " ".join(tokens)
        for field, boost in PHRASE_BOOST_FIELDS:
            parts.append(f'{field}:"{phrase}"^{boost}')

    # 2. Per-token, per-field clauses. Description/tags get a 5x boost
    # over path_tokens so an AI tag match dominates a path-substring
    # match for the same word.
    for token in tokens:
        for field, boost in FIELD_BOOSTS:
            parts.append(f"{field}:{token}^{boost}")

    return " OR ".join(parts)


# ---------------------------------------------------------------------------
# Postgres fallback ranking
# ---------------------------------------------------------------------------

# Order: full-phrase whole-word match > per-token whole-word match > any
# substring match (which is what we already had). Encoded as a SQL
# expression that the fallback search uses in its ORDER BY.
def postgres_rank_clauses(query: str) -> tuple[str, dict[str, str]]:
    """Build SQL expressions + bound params for ranking the Postgres
    fallback. The fallback already retrieves rows by ILIKE; this adds an
    ORDER BY that hoists whole-word matches above incidental substring
    matches.

    Returns ``(order_by_expr, params)``. The order_by_expr is a single
    SQL expression suitable for use in ``ORDER BY {expr}, a.asset_id``.
    Lower values rank higher. ``params`` are bound names referenced by
    the expression — the caller must merge them into the query params
    dict.
    """
    tokens = tokenize(query)
    if not tokens:
        # Stable identity ordering — caller's downstream pagination is
        # asset_id based anyway.
        return "0", {}

    # Build a regex word-boundary pattern that matches the FULL phrase
    # as whole words. PostgreSQL ~* with \m / \M anchors. Escape regex
    # metachars in tokens since users might type them.
    phrase_pattern = r"\m" + r"\s+".join(re.escape(t) for t in tokens) + r"\M"

    # Per-token whole-word: matches if any token appears as a whole word
    # anywhere in the corpus. Used as the second-tier rank floor.
    token_alt = "|".join(re.escape(t) for t in tokens)
    token_pattern = r"\m(" + token_alt + r")\M"

    # Concatenate the searchable text fields once so the regex is cheap
    # — we already JOIN asset_metadata in the fallback query.
    haystack = (
        "COALESCE(m.data->>'description','') || ' ' || "
        "COALESCE(CAST(m.data->'tags' AS TEXT),'') || ' ' || "
        "COALESCE(m.data->>'ocr_text','') || ' ' || "
        "COALESCE(a.note,'') || ' ' || "
        "COALESCE(a.transcript_text,'')"
    )

    # 0 = phrase whole-word match (best), 1 = token whole-word match,
    # 2 = substring-only fallback (the existing ILIKE recall)
    expr = (
        f"CASE "
        f"  WHEN ({haystack}) ~* :rank_phrase THEN 0 "
        f"  WHEN ({haystack}) ~* :rank_token THEN 1 "
        f"  ELSE 2 "
        f"END"
    )
    params = {
        "rank_phrase": phrase_pattern,
        "rank_token": token_pattern,
    }
    return expr, params
