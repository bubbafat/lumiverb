"""What a person typed into search, as words and phrases (src/server/search/text_search.py makes the query).

Quoted text is a phrase (its words in order); everything else is single
words. Each is lowercased and kept to letters (any language's), digits and
hyphens: what the search's own parsing reads as words, never as its syntax.
Words typed joined by `_` or `.` (a file's name, `IMG_7616.JPG`) are one
thing, taken as quoted text is: a phrase, whether or not it was quoted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

# A word as typed: letters (any language's), digits and hyphens, joined to more by `_` or `.` (what's typed
# can't carry a query's own syntax). A word with `_` or `.` in it is a joined word (JOINS).
_WORD_RE = re.compile(r"[^\W_](?:[^\W_]|-)*(?:[_.][^\W_](?:[^\W_]|-)*)*")
JOINS = re.compile(r"[_.]")
# A phrase's words, as a path's are kept (src/server/search/sync.py): split at `_`, `.` and `-`.
_PART_RE = re.compile(r"[^\W_]+")

# A quoted span. Only matches balanced `"..."` pairs; an unmatched
# trailing `"` is left to the free-token pass which strips it.
_QUOTED_RE = re.compile(r'"([^"]*)"')


@dataclass(frozen=True)
class QueryTerm:
    """One parsed unit of a user query: either a free token or a
    phrase. `tokens` are always lowercased and whitespace-stripped (a
    phrase's split at `_`, `.` and `-`): a phrase's `text` is
    `" ".join(tokens)`. `typed`: a phrase's words as typed, when one has
    `_`, `.` or `-` in it (the search's own parsing may read
    `img_7616.jpg` as one word, where a path's are `img 7616 jpg`).
    `joined`: words typed joined outside quotes (`DJI_2026`), a phrase
    whose last word also matches as a prefix, as an unquoted word does."""

    tokens: tuple[str, ...]
    is_phrase: bool
    typed: str | None = None
    joined: bool = False

    @property
    def text(self) -> str:
        return " ".join(self.tokens)


def _phrase(words: list[str], *, joined: bool = False) -> QueryTerm:
    tokens = tuple(p for w in words for p in _PART_RE.findall(w))
    typed = " ".join(words)
    return QueryTerm(tokens, True, typed if typed != " ".join(tokens) else None, joined)


def _free(words: list[str]) -> list[QueryTerm]:
    """Words outside quotes: each a free word, a joined one a phrase."""
    return [_phrase([w], joined=True) if JOINS.search(w) else QueryTerm((w,), is_phrase=False) for w in words]


def parse_query(raw: str) -> list[QueryTerm]:
    """Split raw user input into terms. Text inside balanced double
    quotes becomes a phrase term (tokens must match in order, adjacent);
    everything outside becomes individual free-token terms, but for words
    joined by `_` or `.`, each a phrase. A single-word quoted "phrase"
    collapses to a free token since a one-word phrase is identical to a
    one-word token match.

    Examples:
        parse_query('negative space') →
            [QueryTerm(('negative',), False), QueryTerm(('space',), False)]
        parse_query('"negative space"') →
            [QueryTerm(('negative', 'space'), True)]
        parse_query('"negative space" beach') →
            [QueryTerm(('negative', 'space'), True),
             QueryTerm(('beach',), False)]
        parse_query('IMG_7616.JPG') →
            [QueryTerm(('img', '7616', 'jpg'), True, 'img_7616.jpg', joined=True)]

    Unmatched trailing `"` is treated as a literal — the content after
    the last unmatched quote falls through the regex and its tokens
    are extracted as free tokens.
    """
    lowered = raw.lower()
    terms: list[QueryTerm] = []
    last = 0
    for m in _QUOTED_RE.finditer(lowered):
        terms.extend(_free(_WORD_RE.findall(lowered[last : m.start()])))
        quoted = _WORD_RE.findall(m.group(1))
        if len(quoted) == 1 and not JOINS.search(quoted[0]):
            terms.append(QueryTerm((quoted[0],), is_phrase=False))
        elif quoted:
            terms.append(_phrase(quoted))
        last = m.end()
    terms.extend(_free(_WORD_RE.findall(lowered[last:])))
    return terms


class Piece(NamedTuple):
    """A piece of what was typed: its words (a free word's one), whether it's taken as quoted text is, and
    how to write it back so it reads the same (`said`)."""

    words: tuple[str, ...]
    quoted: bool
    said: str


def pieces(raw: str) -> list[Piece]:
    """What was typed in order, as parse_query reads it, each piece with whether it's taken as quoted (one
    quoted word too): a free word is ((word,), False), quoted text (its words as typed, True), and words
    joined by `_` or `.` as if quoted ((joined,), True), written back unquoted. Quoted text is taken as it
    is, never understood (src/server/search/understand.py)."""
    lowered = raw.lower()
    out: list[Piece] = []

    def free(text_: str) -> None:
        out.extend(Piece((w,), bool(JOINS.search(w)), w) for w in _WORD_RE.findall(text_))

    last = 0
    for m in _QUOTED_RE.finditer(lowered):
        free(lowered[last : m.start()])
        quoted = tuple(_WORD_RE.findall(m.group(1)))
        if quoted:
            out.append(Piece(quoted, True, '"' + " ".join(quoted) + '"'))
        last = m.end()
    free(lowered[last:])
    return out


# The shortest word that also matches as a prefix: `a` would match almost everything.
PREFIX_MIN_LENGTH = 3
