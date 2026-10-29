"""How a search's clips are ranked (Robert, Oct 29: the exact file name is the strongest signal). In tiers, each
deciding first and the next only breaking its ties:

1. It's named: the search is its file name (with or without its extension), else one of its folders' names.
2. How much of the search it matches, a rare word counting more: each unquoted word weighed by how few of the
   clips found have it (log(1 + found / having it)), the share of that weight the clip matches. A word in 745 clips
   ("D") tells little; one in a single clip, a lot. A word that matched nothing weighs nothing.
3. Where it matched (FIELD): a person's own words (a note, a description they wrote) first, then where it was or
   who's recognised in it (a search understood), then the description, tags and scenes, the text read in it and
   what's said, a plug-in's facts, and last its folder and file names.
4. How well: the text search's own score (words together and in order, whole words over their starts).
5. Newest first.

`Rank.score` is what the search shows; `ordered` puts in the final order, newest breaking the ties."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlmodel import Session

from src.server.search.understand import fold

# Where it matched, the more telling the higher: a search's matched_in (text_search.MATCHED_IN, and a search
# understood's place and person), and a description a person wrote.
FIELD = {"note": 6, "corrected": 6, "person": 5, "place": 5, "description": 4, "scene": 4, "text": 3,
         "transcript": 3, "fact": 2, "path": 1}
SPLIT = 2  # a search's words matched across fields: no one field says
FILE, FOLDER = 2, 1  # named: its file's name, a folder's


@dataclass(frozen=True)
class Rank:
    named: int = 0  # FILE, FOLDER or 0
    covered: float = 1.0  # tier 2's share, summed over the searches combined (each must match)
    field: int = 0  # FIELD, summed over the searches combined
    quality: float = 0.0  # the text search's score, summed
    searches: int = 1

    @property
    def coverage(self) -> float:
        return self.covered / self.searches

    @property
    def score(self) -> float:
        """The tiers in one number, each above every value the next can take."""
        return (self.named * 1e7 + round(self.coverage * 100) * 1e4 + min(self.field, 99) * 1e2
                + min(self.quality * 10, 99.0))

    def plus(self, other: Rank) -> Rank:
        """Two searches a clip matches both of (searches combined, or a search understood's parts)."""
        return Rank(max(self.named, other.named), self.covered + other.covered, self.field + other.field,
                    self.quality + other.quality, self.searches + other.searches)


def coverage(matched: Mapping[str, Sequence[bool]], words: int) -> dict[str, float]:
    """Tier 2 for each clip found: which of the search's words it matched → the share of their weight it has."""
    if not words:
        return dict.fromkeys(matched, 1.0)
    having = [sum(1 for m in matched.values() if i < len(m) and m[i]) for i in range(words)]
    found = len(matched)
    weight = [math.log(1 + found / n) if n else 0.0 for n in having]
    whole = sum(weight)
    if not whole:
        return dict.fromkeys(matched, 1.0)
    return {aid: sum(w for i, w in enumerate(weight) if i < len(m) and m[i]) / whole for aid, m in matched.items()}


def typed_names(terms: Iterable[str]) -> list[str]:
    """What a search could name a clip by: each search as typed, quotes around it aside, folded."""
    return [typed for raw in terms if (typed := " ".join(fold(raw).strip().strip('"').split()))]


def named(session: Session, terms: Iterable[str], asset_ids: Sequence[str]) -> dict[str, int]:
    """Tier 1: of these clips, those a search names, FILE (its file's name, or that without its extension) or
    FOLDER (one of its folders' names). Never for a public page's search, which reads no paths."""
    names = typed_names(terms)
    if not names or not asset_ids:
        return {}
    rows = session.execute(text("""
        SELECT a.asset_id,
               CASE WHEN lower(regexp_replace(a.rel_path, '^.*/', '')) = ANY(:names)
                      OR lower(regexp_replace(a.rel_path, '^.*/|\\.[^./]*$', '', 'g')) = ANY(:names) THEN :file
                    WHEN EXISTS (SELECT 1 FROM unnest(:names) n WHERE position('/' || n || '/' IN '/' || lower(a.rel_path)) > 0)
                    THEN :folder END AS named
        FROM assets a WHERE a.asset_id = ANY(:ids)
    """), {"names": names, "ids": list(asset_ids), "file": FILE, "folder": FOLDER}).all()
    return {r.asset_id: r.named for r in rows if r.named}


def corrected(session: Session, asset_ids: Sequence[str]) -> set[str]:
    """Of these clips, those whose description or tags a person wrote (src/server/repository/corrections.py)."""
    if not asset_ids:
        return set()
    return {r[0] for r in session.execute(text(
        "SELECT asset_id FROM asset_corrections WHERE asset_id = ANY(:ids)"
        " AND (description IS NOT NULL OR tags IS NOT NULL)"), {"ids": list(asset_ids)})}


def ordered(session: Session, ranks: Mapping[str, Rank]) -> dict[str, float]:
    """The clips' places in the final order as scores (the first highest): by Rank.score, then newest first (its
    taken time, else its file's), then by id."""
    if not ranks:
        return {}
    taken = {r.asset_id: r.at for r in session.execute(text(
        "SELECT asset_id, COALESCE(taken_at, file_mtime) AS at FROM assets WHERE asset_id = ANY(:ids)"),
        {"ids": list(ranks)})}
    order = sorted(ranks, key=lambda aid: (-ranks[aid].score, -(taken[aid].timestamp()) if taken.get(aid) else 0.0,
                                           aid))
    return {aid: float(len(order) - i) for i, aid in enumerate(order)}
