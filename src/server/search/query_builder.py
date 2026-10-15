"""What a person typed into search, as words and phrases (src/server/search/text_search.py makes the query).

Quoted text is a phrase (its words in order); everything else is single
words. Each is lowercased and kept to letters, digits and hyphens: what
the search's own parsing reads as words, never as its syntax.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The characters a word keeps: letters, digits and hyphens (what's typed
# can't carry a query's own syntax).
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

# A quoted span. Only matches balanced `"..."` pairs; an unmatched
# trailing `"` is left to the free-token pass which strips it.
_QUOTED_RE = re.compile(r'"([^"]*)"')


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


# The shortest word that also matches as a prefix: `a` would match almost everything.
PREFIX_MIN_LENGTH = 3
