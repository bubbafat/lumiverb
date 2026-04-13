"""Query construction for Quickwit and Postgres text search.

The default Quickwit query (`combined_query`) just dumps the raw user
input into the engine, which then searches every default field with
equal weight. That made search underperform: an OCR'd street sign
saying "card" outranked a photo whose AI description was literally
"Christmas card" because BM25 had no field-level signal to break the
tie. The schema's `default` tokenizer is already whole-word, so the
fix isn't about tokenization — it's about teaching the engine that a
match in `description`/`tags` is more authoritative than a match in
`ocr_text`/`path_tokens`.

**Why this file doesn't use Quickwit boost syntax (`field:term^N`):**
The first attempt did, and Quickwit silently returned 0 hits, which
fell through to the Postgres ILIKE fallback (which has no scoring and
matches `%card%` against "cardboard"). Quickwit's REST query parser is
NOT the full Tantivy parser — boosts aren't reliably supported. The
safe approach is field-restricted OR clauses without boosts, then we
do the actual *ranking* in two ways:

1. The query string includes high-priority fields (description, tags,
   note) FIRST. BM25 scores naturally favor matches in those fields
   because they're tighter (lower fieldnorm) than ocr_text / path
   tokens.
2. For multi-token queries we add explicit phrase clauses (`"greeting
   card"`). Phrases are rarer than tokens → higher IDF → naturally
   outrank per-token matches.

It also exposes a small helper for ranking the Postgres ILIKE fallback
by whole-word presence so the fallback path doesn't undo the gains.
"""

from __future__ import annotations

import re

# Fields searched for a per-token match, in priority order. Quickwit
# scores all of them with BM25 — putting high-signal fields in this
# list (vs leaving them to default_search_fields) targets recall to
# the right places without depending on boost syntax.
QUERY_FIELDS: list[str] = [
    "description",
    "tags",
    "note",
    "transcript_text",
    "ocr_text",
    "path_tokens",
]

# Fields where a phrase match is meaningful. Excludes ocr_text and
# path_tokens because phrases in those fields are noisy / accidental.
PHRASE_FIELDS: list[str] = ["description", "tags", "note"]

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
    """Build a field-restricted Quickwit query from raw user input.

    Returns an empty string when the input has no usable tokens — the
    caller should treat that as "no text search" and skip the engine
    entirely rather than sending a malformed query.

    Uses no boost syntax (Quickwit's REST parser doesn't reliably
    support it). Ranking comes from BM25 over the explicit field set
    plus phrase clauses for multi-token queries.
    """
    tokens = tokenize(raw)
    if not tokens:
        return ""

    parts: list[str] = []

    # 1. Phrase clauses — multi-token queries get exact-phrase matches
    # in high-signal fields. Phrases are rarer than tokens so BM25
    # naturally ranks them higher via IDF, no boost needed.
    if len(tokens) > 1:
        phrase = " ".join(tokens)
        for field in PHRASE_FIELDS:
            parts.append(f'{field}:"{phrase}"')

    # 2. Per-token, per-field clauses. Listing description/tags/note
    # before ocr_text/path_tokens doesn't change BM25 scoring on its
    # own — Quickwit ORs them all — but it keeps the query readable
    # and makes the recall set explicit. Whole-word matches in any
    # listed field are kept; substring "card" → "cardboard" no longer
    # leaks in via the Postgres ILIKE fallback because Quickwit
    # actually returns hits now.
    for token in tokens:
        for field in QUERY_FIELDS:
            parts.append(f"{field}:{token}")

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
