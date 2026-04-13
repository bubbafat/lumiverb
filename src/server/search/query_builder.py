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
from dataclasses import dataclass

# Per-index field profiles. Quickwit's three indexes (asset / scene /
# transcript) have *different* schemas, and a query that names a field
# the target index doesn't define returns 400 from Quickwit's parser.
# That 400 used to crash the whole search into the Postgres fallback —
# the asset index would match plenty, but the transcript index would
# choke on `description:` and the exception caught in
# `_run_quickwit_search` would discard everything. So we now build a
# different query per index with the fields it actually has.
ASSET_FIELDS: list[str] = [
    "description",
    "tags",
    "note",
    "transcript_text",
    "ocr_text",
    "path_tokens",
]
ASSET_PHRASE_FIELDS: list[str] = ["description", "tags", "note"]

SCENE_FIELDS: list[str] = ["description", "tags"]
SCENE_PHRASE_FIELDS: list[str] = ["description", "tags"]

# Transcript schema only stores `text`. The transcript_text on the
# asset index is asset-level full transcript; the per-segment text
# lives here under a different field name.
TRANSCRIPT_FIELDS: list[str] = ["text"]
TRANSCRIPT_PHRASE_FIELDS: list[str] = ["text"]

# Backwards-compat alias used by older callers / tests that don't pass
# explicit field lists. Defaults to the asset profile (the broadest).
QUERY_FIELDS: list[str] = ASSET_FIELDS
PHRASE_FIELDS: list[str] = ASSET_PHRASE_FIELDS

# Whitelist of characters allowed in a token. Quickwit's query parser is
# strict about reserved characters (`:`, `^`, `(`, `"` etc.), and the
# raw user input would inject syntax. Lowercase the input and keep only
# alphanumerics + hyphens — that matches what the `default` tokenizer
# stores in the index anyway.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

# A quoted span. Only matches balanced `"..."` pairs; an unmatched
# trailing `"` is left to the free-token pass which strips it.
_QUOTED_RE = re.compile(r'"([^"]*)"')


def tokenize(query: str) -> list[str]:
    """Lowercase + extract tokens. Mirrors what Quickwit's `default`
    tokenizer would do at index time, so the tokens we send for matching
    line up with what's actually stored. Ignores quotes — use
    `parse_query` when you need to preserve phrase structure."""
    return _TOKEN_RE.findall(query.lower())


@dataclass(frozen=True)
class QueryTerm:
    """One parsed unit of a user query: either a free token or a
    quoted phrase. `tokens` are always lowercased and whitespace-
    stripped — a phrase's `text` is `" ".join(tokens)`."""

    tokens: tuple[str, ...]
    is_phrase: bool

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


def parse_query(raw: str) -> list[QueryTerm]:
    """Split raw user input into terms. Text inside balanced double
    quotes becomes a phrase term (tokens must match in order, adjacent);
    everything outside becomes individual free-token terms. A
    single-token "phrase" collapses to a free token since a one-word
    phrase is identical to a one-word token match.

    Examples:
        parse_query('negative space') →
            [QueryTerm(('negative',), False), QueryTerm(('space',), False)]
        parse_query('"negative space"') →
            [QueryTerm(('negative', 'space'), True)]
        parse_query('"negative space" beach') →
            [QueryTerm(('negative', 'space'), True),
             QueryTerm(('beach',), False)]

    Unmatched trailing `"` is treated as a literal — the content after
    the last unmatched quote falls through the regex and its tokens
    are extracted as free tokens.
    """
    lowered = raw.lower()
    terms: list[QueryTerm] = []
    last = 0
    for m in _QUOTED_RE.finditer(lowered):
        for tok in _TOKEN_RE.findall(lowered[last : m.start()]):
            terms.append(QueryTerm((tok,), is_phrase=False))
        phrase_tokens = tuple(_TOKEN_RE.findall(m.group(1)))
        if phrase_tokens:
            terms.append(
                QueryTerm(phrase_tokens, is_phrase=len(phrase_tokens) > 1)
            )
        last = m.end()
    for tok in _TOKEN_RE.findall(lowered[last:]):
        terms.append(QueryTerm((tok,), is_phrase=False))
    return terms


def build_quickwit_query(
    raw: str,
    fields: list[str] | None = None,
    phrase_fields: list[str] | None = None,
) -> str:
    """Build a field-restricted Quickwit EXACT query from raw user
    input. Includes per-token clauses and (for multi-token queries)
    phrase clauses, but NO prefix wildcards — see
    `build_quickwit_prefix_query` for that.

    Uses no boost syntax (Quickwit's REST parser doesn't reliably
    support it). Ranking comes from BM25 over the explicit field set
    plus phrase clauses for multi-token queries.

    **Quoted phrases**: text wrapped in `"..."` is required — the
    phrase must match (across any searchable field) as a contiguous
    sequence. A mix like `"negative space" beach` becomes
    `( phrase-across-fields ) AND ( beach-or-clause )`. Without any
    quotes the output is a flat OR across per-token and phrase
    clauses, preserving the loose ranking behavior.

    `fields` and `phrase_fields` default to the asset-index profile
    so existing callers keep working. Pass index-specific lists when
    targeting the scene or transcript index — naming a field the
    target index doesn't define will make Quickwit return 400 and
    drop the entire search into the Postgres fallback.

    Returns an empty string when the input has no usable tokens — the
    caller should treat that as "no text search" and skip the engine
    entirely rather than sending a malformed query.
    """
    terms = parse_query(raw)
    if not terms:
        return ""

    use_fields = fields if fields is not None else QUERY_FIELDS
    use_phrase_fields = phrase_fields if phrase_fields is not None else PHRASE_FIELDS

    quoted = [t for t in terms if t.is_phrase]
    free_tokens = [t.tokens[0] for t in terms if not t.is_phrase]

    def _free_or_group() -> str:
        parts: list[str] = []
        # Multi-token free runs still get an automatic phrase clause
        # on the high-signal fields so an unquoted "greeting card"
        # keeps ranking the whole phrase above incidental token hits.
        if len(free_tokens) > 1:
            phrase = " ".join(free_tokens)
            for field in use_phrase_fields:
                parts.append(f'{field}:"{phrase}"')
        for token in free_tokens:
            for field in use_fields:
                parts.append(f"{field}:{token}")
        return " OR ".join(parts)

    # No required phrases → preserve the flat OR shape so existing
    # behavior is unchanged when the user doesn't use quotes.
    if not quoted:
        return _free_or_group()

    # Each quoted phrase is REQUIRED: it must match in at least one
    # searchable field. The phrase clause spans ALL use_fields, not
    # just phrase_fields, because the user's explicit quoting means
    # they want exactness wherever the phrase appears.
    required_clauses: list[str] = []
    for phrase_term in quoted:
        phrase = phrase_term.text
        phrase_parts = [f'{field}:"{phrase}"' for field in use_fields]
        required_clauses.append("(" + " OR ".join(phrase_parts) + ")")

    # Free tokens mixed with quoted phrases are also required —
    # conventional search UX treats `"foo bar" baz` as "must have
    # the phrase AND must have baz".
    if free_tokens:
        required_clauses.append("(" + _free_or_group() + ")")

    return " AND ".join(required_clauses)


# Minimum token length for prefix expansion. `a*` would match almost
# everything in the corpus and explode the candidate set.
PREFIX_MIN_LENGTH = 3


def build_quickwit_prefix_query(
    raw: str,
    fields: list[str] | None = None,
) -> str:
    """Build a Quickwit PREFIX-ONLY query so a search for "disney"
    surfaces "disneyland" / "disneyworld" / "disney+" via wildcard
    expansion. Issued as a SEPARATE Quickwit call from the exact
    query (not OR'd into one query string) because Quickwit's
    wildcard queries get constant scoring rather than BM25 — when
    OR'd together, exact matches always dominate prefix-only matches
    by a huge margin and the prefix-only docs end up buried (we saw
    Disneyland landing at result #103 of an OR'd query for "disney").

    The caller fuses the two result lists with a position-based score
    and a penalty on the prefix list — see `_run_quickwit_search`.

    Skips path_tokens because directory-component prefixes are noisy.
    Skips tokens shorter than `PREFIX_MIN_LENGTH`. Tokens inside a
    quoted phrase are treated as exact — no prefix expansion — so a
    pure quoted-phrase query returns "" and the caller skips the
    prefix pass entirely.
    """
    terms = parse_query(raw)
    if not terms:
        return ""

    use_fields = fields if fields is not None else QUERY_FIELDS
    prefix_fields = [f for f in use_fields if f != "path_tokens"]

    # Only free (unquoted) tokens get prefix expansion.
    free_tokens = [t.tokens[0] for t in terms if not t.is_phrase]

    parts: list[str] = []
    for token in free_tokens:
        if len(token) < PREFIX_MIN_LENGTH:
            continue
        for field in prefix_fields:
            parts.append(f"{field}:{token}*")

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
    terms = parse_query(query)
    if not terms:
        # Stable identity ordering — caller's downstream pagination is
        # asset_id based anyway.
        return "0", {}

    all_tokens = [tok for t in terms for tok in t.tokens]
    if not all_tokens:
        return "0", {}

    # Phrase pattern: if the user explicitly quoted phrases, rank docs
    # that contain any of those phrases as whole words. Otherwise fall
    # back to ranking the full free-token sequence as a contiguous
    # phrase (preserving the pre-quoting behavior). Escape regex metas
    # so user input can't inject pattern syntax.
    quoted = [t for t in terms if t.is_phrase]
    if quoted:
        phrase_alts = [
            r"\m" + r"\s+".join(re.escape(tok) for tok in t.tokens) + r"\M"
            for t in quoted
        ]
        phrase_pattern = "(" + "|".join(phrase_alts) + ")"
    else:
        phrase_pattern = (
            r"\m" + r"\s+".join(re.escape(t) for t in all_tokens) + r"\M"
        )

    # Per-token whole-word: matches if any token appears as a whole word
    # anywhere in the corpus. Used as the second-tier rank floor.
    token_alt = "|".join(re.escape(t) for t in all_tokens)
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
