"""Text search in PostgreSQL (ADR-019): the clips, scenes and transcript segments a search's words match, and how well.

Every text search is here, so the engine is this module's alone: ParadeDB's
BM25 (pg_search), say, would be a migration and this file. What's searched
is kept beside what it's made from, in the account's database:

- a clip's words: `asset_search` (src/server/search/sync.py keeps each clip's
  row as a person sees it), in three columns by who's asking: `everything`
  (the account's own), `shown` (a public page: no notes, no paths) and
  `shown_capped` (a public page that plays only the start of a video: no
  transcript either);
- a scene's: `video_scenes.words`, computed with the row (its description and
  tags), for the scenes a clip shows (not a redo's staged ones);
- a transcript's: `transcript_segments`, one row a segment, with its times;
- a plug-in's facts (ADR-023): `plugin_outputs.words`, the text of the
  facts its manifest says are searchable, for the plug-ins on in the account
  (the caller says which: never on a public page).

The words are read as English, in any language's letters: stemmed, so "dogs"
finds "dog", a path's words too (a folder "Family" is found by "family", whose
stem is "famili"); accents aside, so "zurich" finds "Zürich" and the other way
round (what's shown keeps them); a hyphenated word as its parts, so a quoted
"John Wayne Airport" finds "John Wayne Airport-Orange County" (the
configuration `lumiverb`, migration c7e1a9d3f5b2). Typed words are read the
same way (src/server/search/query_builder.py parses what a person typed). Unquoted words: any of them
matches, more of them ranks higher, and all of them in a row higher still.
A quoted phrase must match, its words in order. Words of three letters or
more match as a prefix too ("disney" finds "Disneyland"), ranked at half.
Matches in a description, tags or a note weigh most, then a plug-in's
facts, then the text read in a picture and a transcript, then a path. Several search terms (filters) must
each match.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from src.server.search.query_builder import PREFIX_MIN_LENGTH, parse_query

CONFIG = "lumiverb"  # English's, accents aside and a hyphenated word by its parts
# A prefix match ranks at half an exact one.
PREFIX_PENALTY = 0.5
# A prefix is only a whole token of letters and digits (any language's): to_tsquery reads anything else as syntax.
_PREFIXABLE = re.compile(r"[^\W_]+")
# How big a search can be: far more than anyone types (a long search is a handful of words, and a search
# stacked on another two or three), small enough that no search, a public page's included, is a query too
# big to run (each word is two tsqueries in it, and each row's rank weighs them all).
MAX_WORDS = 32  # in one search: its words, a quoted phrase's each
MAX_TERMS = 16  # searches that must each match (query: filters)


class SearchTooLongError(ValueError):
    """A search past MAX_WORDS or MAX_TERMS: refused (GET /v1/query's 400 search_too_long), saying which."""


@dataclass(frozen=True)
class Hit:
    asset_id: str
    score: float
    snippet: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class Query:
    """A search's tsquery expressions (SQL over bound parameters): `exact`
    what must match, `loose` the same with each word also a prefix, `phrase`
    each term's unquoted words in a row (ranked, never required)."""

    exact: str
    loose: str
    phrase: str | None
    params: dict[str, str]


def build(terms: list[str], also: Sequence[Sequence[str]] | None = None) -> Query | None:
    """The tsquery for search terms (each what a person typed; all must
    match), or None when none has a word. also: for each term, other words or
    phrases that match as its unquoted words do (an AI-assisted search's
    things of a kind: src/server/search/assist.py), never counted against its
    size. SearchTooLongError past MAX_TERMS terms, or a term past MAX_WORDS words."""
    if len(terms) > MAX_TERMS:
        raise SearchTooLongError(f"At most {MAX_TERMS} searches can be combined; this has {len(terms)}.")
    params: dict[str, str] = {}

    def bind(value: str) -> str:
        name = f"w{len(params)}"
        params[name] = value
        return f":{name}"

    def word(token: str) -> str:
        return f"plainto_tsquery('{CONFIG}', {bind(token)})"

    def prefix(token: str) -> str:
        if len(token) < PREFIX_MIN_LENGTH or not _PREFIXABLE.fullmatch(token):
            return word(token)
        return f"to_tsquery('{CONFIG}', {bind(token + ':*')})"

    def phrase(tokens: tuple[str, ...]) -> str:
        return f"phraseto_tsquery('{CONFIG}', {bind(' '.join(tokens))})"

    exact: list[str] = []
    loose: list[str] = []
    phrases: list[str] = []
    for n, raw in enumerate(terms):
        parsed = parse_query(raw)
        if not parsed:
            continue
        words = sum(len(t.tokens) for t in parsed)
        if words > MAX_WORDS:
            raise SearchTooLongError(f"A search is at most {MAX_WORDS} words; this one has {words}. "
                                     "Leave some out.")
        quoted = [t.tokens for t in parsed if t.is_phrase]
        free = [t.tokens[0] for t in parsed if not t.is_phrase]
        required = [phrase(q) for q in quoted]
        others = [tuple(t for p in parse_query(alt) for t in p.tokens) for alt in (also[n] if also else ())]
        for clauses, one in ((exact, word), (loose, prefix)):
            group = [one(t) for t in free] + [phrase(o) if len(o) > 1 else one(o[0]) for o in others if o]
            parts = required + ([f"({' || '.join(group)})"] if group else [])
            clauses.append("(" + " && ".join(parts) + ")")
        if len(free) > 1:
            phrases.append(phrase(tuple(free)))
    if not exact:
        return None
    return Query(exact=" && ".join(exact), loose=" && ".join(loose),
                 phrase=" || ".join(phrases) if phrases else None, params=params)


def _score(column: str, q: Query, *, loose: bool) -> str:
    """How well a row's words match: exact matches, prefix-only ones at
    PREFIX_PENALTY, plus its words in a row."""
    score = f"ts_rank_cd({column}, {q.exact})"
    if loose:
        score = f"GREATEST({score}, {PREFIX_PENALTY} * ts_rank_cd({column}, {q.loose}))"
    if q.phrase:
        score += f" + ts_rank_cd({column}, {q.phrase})"
    return score


def _in(library_ids: list[str] | None, params: dict) -> str:
    if library_ids is None:
        return ""
    params["library_ids"] = list(library_ids)
    return " AND a.library_id = ANY(CAST(:library_ids AS text[]))"


def clips(session: Session, q: Query, library_ids: list[str] | None, limit: int, *,
          public: bool = False, public_cap_ms: int | None = None) -> list[Hit]:
    """The clips in sight whose own words match (best first), each with
    its description as the snippet. A public page never reads notes or
    paths, and one that plays only the start of a video no transcript."""
    column = "s." + ("shown_capped" if public_cap_ms is not None else "shown" if public else "everything")
    params = {**q.params, "limit": limit}
    rows = session.execute(text(f"""
        SELECT s.asset_id, s.snippet, {_score(column, q, loose=True)} AS score
        FROM asset_search s JOIN active_assets a ON a.asset_id = s.asset_id
        WHERE {column} @@ ({q.loose}){_in(library_ids, params)}
        ORDER BY score DESC, s.asset_id LIMIT :limit
    """), params).all()
    return [Hit(r.asset_id, float(r.score), r.snippet or None) for r in rows]


def facts(session: Session, q: Query, library_ids: list[str] | None, limit: int,
          plugins: dict[str, str]) -> list[Hit]:
    """The clips in sight with a fact of one of these plug-ins whose words match (best first), each with
    those facts' text as the snippet. plugins: each plug-in's id and the SQL on active_assets a for the
    clips whose facts it shows (repository/plugin_outputs.py searched_plugins)."""
    if not plugins:
        return []
    params = {**q.params, "limit": limit}
    shown = []
    for n, (plugin_id, where) in enumerate(sorted(plugins.items())):
        params[f"plugin{n}"] = plugin_id
        shown.append(f"(po.plugin_id = :plugin{n} AND {where})")
    rows = session.execute(text(f"""
        SELECT po.asset_id, po.search_text, {_score('po.words', q, loose=True)} AS score
        FROM plugin_outputs po JOIN active_assets a ON a.asset_id = po.asset_id
        WHERE ({' OR '.join(shown)}) AND po.words @@ ({q.loose}){_in(library_ids, params)}
        ORDER BY score DESC, po.asset_id LIMIT :limit
    """), params).all()
    return [Hit(r.asset_id, float(r.score), r.search_text or None) for r in rows]


def scenes(session: Session, q: Query, library_ids: list[str] | None, limit: int, *,
           public_cap_ms: int | None = None) -> list[Hit]:
    """The scenes a clip in sight shows whose description or tags match
    (best first), each with its times; a capped public page's, only those
    starting within what it plays."""
    params = {**q.params, "limit": limit}
    capped = ""
    if public_cap_ms is not None:
        capped = " AND v.start_ms < :cap"
        params["cap"] = public_cap_ms
    rows = session.execute(text(f"""
        SELECT v.asset_id, v.description, v.start_ms, v.end_ms, {_score('v.words', q, loose=True)} AS score
        FROM video_scenes v JOIN active_assets a ON a.asset_id = v.asset_id
        WHERE v.words @@ ({q.loose}) AND v.staged IS NULL AND v.description IS NOT NULL{capped}{_in(library_ids, params)}
        ORDER BY score DESC, v.scene_id LIMIT :limit
    """), params).all()
    return [Hit(r.asset_id, float(r.score), r.description, r.start_ms, r.end_ms) for r in rows]


def segments(session: Session, q: Query, library_ids: list[str] | None, limit: int, *,
             public_cap_ms: int | None = None) -> list[Hit]:
    """The transcript segments of clips in sight whose words match exactly
    (best first: no prefixes, a transcript has too many words), each with
    its text and times; a capped public page's, only those starting within
    what it plays."""
    params = {**q.params, "limit": limit}
    capped = ""
    if public_cap_ms is not None:
        capped = " AND t.start_ms < :cap"
        params["cap"] = public_cap_ms
    rows = session.execute(text(f"""
        SELECT t.asset_id, t.text, t.start_ms, t.end_ms, {_score('t.words', q, loose=False)} AS score
        FROM transcript_segments t JOIN active_assets a ON a.asset_id = t.asset_id
        WHERE t.words @@ ({q.exact}){capped}{_in(library_ids, params)}
        ORDER BY score DESC, t.asset_id, t.seq LIMIT :limit
    """), params).all()
    return [Hit(r.asset_id, float(r.score), r.text, r.start_ms, r.end_ms) for r in rows]
