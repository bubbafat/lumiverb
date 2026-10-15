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
- a transcript's: `transcript_segments`, one row a segment, with its times.

The words are English: stemmed, so "dogs" finds "dog", a path's words too
(a folder "Family" is found by "family", whose stem is "famili"), as typed
words are read the same way (src/server/search/query_builder.py parses what a
person typed). Unquoted words: any of them
matches, more of them ranks higher, and all of them in a row higher still.
A quoted phrase must match, its words in order. Words of three letters or
more match as a prefix too ("disney" finds "Disneyland"), ranked at half.
Matches in a description, tags or a note weigh most, then the text read in a
picture and a transcript, then a path. Several search terms (filters) must
each match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from src.server.search.query_builder import PREFIX_MIN_LENGTH, parse_query

CONFIG = "english"
# A prefix match ranks at half an exact one.
PREFIX_PENALTY = 0.5
# A prefix is only a whole token of letters and digits: to_tsquery reads anything else as syntax.
_PREFIXABLE = re.compile(r"^[a-z0-9]+$")


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


def build(terms: list[str]) -> Query | None:
    """The tsquery for search terms (each what a person typed; all must
    match), or None when none has a word."""
    params: dict[str, str] = {}

    def bind(value: str) -> str:
        name = f"w{len(params)}"
        params[name] = value
        return f":{name}"

    def word(token: str) -> str:
        return f"plainto_tsquery('{CONFIG}', {bind(token)})"

    def prefix(token: str) -> str:
        if len(token) < PREFIX_MIN_LENGTH or not _PREFIXABLE.match(token):
            return word(token)
        return f"to_tsquery('{CONFIG}', {bind(token + ':*')})"

    def phrase(tokens: tuple[str, ...]) -> str:
        return f"phraseto_tsquery('{CONFIG}', {bind(' '.join(tokens))})"

    exact: list[str] = []
    loose: list[str] = []
    phrases: list[str] = []
    for raw in terms:
        parsed = parse_query(raw)
        if not parsed:
            continue
        quoted = [t.tokens for t in parsed if t.is_phrase]
        free = [t.tokens[0] for t in parsed if not t.is_phrase]
        required = [phrase(q) for q in quoted]
        for clauses, one in ((exact, word), (loose, prefix)):
            group = [one(t) for t in free]
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
