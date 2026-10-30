"""Unified query endpoint — replaces /v1/browse and /v1/search.

Accepts filter algebra via repeated ?f=prefix:value params. When a SearchTerm
filter is present, uses the candidate-set pattern: the text search
(src/server/search/text_search.py) returns candidate IDs + scores, then the
structured filters and pagination are applied to them.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_optional_user_id, get_tenant_session
from src.server.api.library_access import seen
from src.server.api.limits import MAX_PAGE
from src.server.api.rate_limit import anonymous_search_limited
from src.server.api.errors import ApiError
from src.server.api.routers.projects import guard_project_filters
from src.server.models.filter_registry import parse_f_params
from src.server.models.query_filter import (
    LOCATION_FILTERS,
    ApertureRange,
    CameraMake,
    CameraModel,
    Combinator,
    ExposureRange,
    FocalLengthRange,
    GroupFilter,
    HasExposure,
    IsoRange,
    LensModel,
    LibraryScope,
    PathPrefix,
    PersonFilter,
    QuerySpec,
    SearchTerm,
)
from src.server.repository.browse import UnifiedBrowseRepository
from src.server.repository.libraries import LibraryRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/query", tags=["query"])

SORT_COLUMNS = {
    "taken_at", "created_at", "changed_at", "file_size", "iso",
    "exposure_time_us", "aperture", "focal_length", "rel_path", "asset_id",
}
# A cursor carries the last row's sort value: a visitor's mustn't carry a file name or EXIF.
PUBLIC_SORTS = {"taken_at", "asset_id"}

MAX_CANDIDATE_IDS = 5000
MAX_BEST = 50  # a search's best matches on its first page, at most (best=N)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SearchContext(BaseModel):
    score: float
    hit_type: str = "asset"
    snippet: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    # What found it: its description, note, text (read in it), transcript (a segment's has its times), path
    # (its folder or file name), fact (a plug-in's: from_plugin its title), scene (with its times); a signed-in
    # search's place (where it was: snippet the place) or person (who's recognised in it: snippet their name).
    # None when no one field says (a search's words split between them). The snippet is an excerpt of it.
    matched_in: str | None = None
    from_plugin: str | None = None


class QueryItem(BaseModel):
    asset_id: str
    library_id: str
    library_name: str
    rel_path: str
    file_size: int
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None
    status: str = "pending"
    duration_sec: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    iso: int | None = None
    aperture: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    lens_model: str | None = None
    flash_fired: bool | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    face_count: int | None = None
    thumbnail_key: str | None = None
    proxy_key: str | None = None
    created_at: str | None = None
    # Its last modified time: the most recent of every time related to it (assets.changed_at).
    changed_at: str | None = None
    search_context: SearchContext | None = None


class UnderstoodPart(BaseModel):
    """One thing a search was understood to ask for (src/server/search/understand.py)."""

    kind: str  # person, place, kind (videos or photos), words (the rest, searched as words)
    words: str  # what was typed for it
    label: str  # what it was taken for: "Sue Horvick", "California Adventure", "videos"


class BestMatches(BaseModel):
    """A search's best matches (src/server/search/rank.py `best`: named by it, or matching all of it), the first
    by relevance, and how many there are with the page's filters."""

    items: list[QueryItem]
    total: int


class QueryResponse(BaseModel):
    items: list[QueryItem]
    next_cursor: str | None = None
    total_estimate: int | None = None
    # Which search produced the candidate set: "postgres" (PostgreSQL's
    # full-text search, ADR-019), or "none" (search terms without a word
    # in them). None when no text search was run at all.
    search_source: str | None = None
    # What the search was understood to ask for, in order; None when nothing
    # was (its words searched as they always were).
    understood: list[UnderstoodPart] | None = None
    # Asked for (best=N), on a search's first page: its best matches.
    best: BestMatches | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_cursor(sort_col: str, sort_value: object, asset_id: str) -> str:
    payload = json.dumps({"v": sort_value, "id": asset_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def parsed_filters(f: list[str], *, sort: str = "taken_at", direction: str = "desc") -> QuerySpec:
    """The ?f= params as a QuerySpec (GET /v1/query, facets)."""
    return parse_f_params(f, sort=sort, direction=direction)


def _run_text_search(
    session: Session,
    search_terms: list[SearchTerm],
    library_ids: list[str] | None,
    limit: int,
    public_cap_ms: int | None = None,
    public: bool = False,
    capped: set[str] | None = None,
    understood: list[dict] | None = None,
    assist_for: str | None = None,
    best: list[str] | None = None,
) -> tuple[dict[str, float], dict[str, SearchContext], str]:
    """The clips a text search finds, in the account's database
    (src/server/search/text_search.py): their own words, their scenes' and
    their transcripts'; for a signed-in search, by who's in them, where they
    were and what kind they are too, where its words say
    (src/server/search/understand.py). `understood`: when given, gets what
    each search was understood to ask for (nothing when none was).
    `assist_for`: the account whose AI machine may be asked about the words
    (an AI-assisted search, `assisting`: src/server/search/assist.py).
    `best`: when given, gets the best matches' ids in order (rank.best).

    `capped`: when given, gets the name of each search that came back full
    (its limit): matches past it were left out. Each search is capped on
    its own and counts rows (scenes, segments), not clips, so the merged
    scores alone can't tell.

    `public_cap_ms`: for a public page that plays only so much of each
    video, search what it shows: no whole-transcript words, and no scene or
    transcript hits from past that point. `public`: a public page's search
    never reads notes (the team's working notes) or paths. Nor a plug-in's
    facts, which the account's own search reads for the plug-ins on in it.

    Returns:
      - scores: {asset_id: best_score}
      - contexts: {asset_id: SearchContext} (what matched: the clip's own
        words, a scene or a transcript segment, with its times)
      - source: "postgres", or "none" for terms without a word
    """
    from src.server.search import text_search

    terms = [st.q for st in search_terms if st.q]
    try:  # too many words or searches: refused before anything is read
        has_words = text_search.build(terms) is not None
    except text_search.SearchTooLongError as exc:  # said as the search box shows it
        raise ApiError(400, "search_too_long", str(exc),
                       {"max_words": text_search.MAX_WORDS, "max_searches": text_search.MAX_TERMS}) from None
    if not has_words:
        return {}, {}, "none"  # not a word in it: nothing is found, and nothing read
    if not public and public_cap_ms is None:
        from src.server.search import understand

        read = [understand.read(session, t, library_ids) for t in terms]
        things: dict[str, dict[str, list[str]]] = {}
        if assist_for:
            from src.server.search import assist

            model, ask = assist.asker(assist_for)
            asked = list(dict.fromkeys(a for u in read if (a := assist.asked_about(u.rest))))
            things = assist.kinds(session, asked, model, ask) if model else {}
        if any(u.parts for u in read) or things:
            if understood is not None:
                understood.extend(part for u in read for part in u.said())
                understood.extend({"kind": "also", "words": kind,
                                   "label": ", ".join(t[:5]) + (", …" if len(t) > 5 else "")}
                                  for found in things.values() for kind, t in found.items())
            return _ordered(session, terms, *_understood_search(session, read, library_ids, limit, capped, things),
                            public=False, best=best)
    return _ordered(session, terms, *_words_search(session, terms, library_ids, limit, public_cap_ms, public, capped),
                    public=public or public_cap_ms is not None, best=best)


def _ordered(session: Session, terms: list[str], ranks: dict, contexts: dict[str, SearchContext], source: str, *,
             public: bool, best: list[str] | None = None) -> tuple[dict[str, float], dict[str, SearchContext], str]:
    """The clips found in their final order (src/server/search/rank.py): named by the search first (never on a
    public page, which reads no paths), each context's score its rank's; best gets the best matches."""
    from dataclasses import replace

    from src.server.search import rank

    if not public:
        named = rank.named(session, terms, list(ranks))
        ranks = {aid: replace(r, named=named[aid]) if aid in named else r for aid, r in ranks.items()}
    contexts = {aid: c.model_copy(update={"score": ranks[aid].score}) for aid, c in contexts.items() if aid in ranks}
    order = rank.ordered(session, ranks)
    if best is not None:
        best.extend(rank.best(ranks, order))
    return order, contexts, source


def assisting(request: Request, session: Session, spec: QuerySpec) -> str | None:
    """The account whose AI machine a search may ask about its words: when it says so (assist:yes) and the account
    lets searches ask (Settings › AI); never a public page's."""
    from src.server.models.query_filter import AssistedSearch
    from src.server.tenant_settings import get_assisted_search

    if getattr(request.state, "is_public_request", False):
        return None
    if not any(isinstance(leaf, AssistedSearch) and leaf.value for leaf in spec.leaves):
        return None
    return request.state.tenant_id if get_assisted_search(session) else None


def _understood_search(session: Session, read: list, library_ids: list[str] | None, limit: int,
                       capped: set[str] | None, things: dict[str, dict[str, list[str]]] | None = None,
                       ) -> tuple[dict, dict[str, SearchContext], str]:
    """Understood searches (each an understand.Understood), every part of each to match: a person or a
    place by who's recognised in a clip or where it was, or by its words; the kind asked for. Ranked
    (src/server/search/rank.py) by the words left (each search's, combined); with none left, by the parts:
    where it was or who's in it above a word naming them. things: the kinds each search's words name, with
    their things, searched with them (an AI-assisted search's: assist.kinds)."""
    from src.server.search import assist, rank, understand

    ranks: dict[str, rank.Rank] | None = None
    contexts: dict[str, SearchContext] = {}
    for u in read:
        found: dict[str, rank.Rank] | None = None  # None: nothing has narrowed it yet
        if u.rest:
            also = assist.by_word((things or {}).get(assist.asked_about(u.rest), {}))
            found, said, _ = _words_search(session, [u.rest], library_ids, limit, None, False, capped, [also])
            for aid, c in said.items():
                contexts.setdefault(aid, c)
        for part in u.parts:
            if part.kind == understand.KIND:
                continue
            # Each clip found by who's in it or where it was, with what found it: their name, or the place it's
            # near (of several by one name, which).
            if part.kind == understand.PERSON:
                known = dict.fromkeys(understand.clips_with(session, part.person_ids, library_ids), part.label)
            else:
                near = understand.clips_near(session, part.places, library_ids)
                known = {aid: p.shown if len(part.places) > 1 else part.label for aid, p in near.items()}
            by_words, said, _ = _words_search(session, [part.searched], library_ids, limit, None, False, capped)
            # Found by who's in it or where it was ranks above a word naming them; someone named by another of their
            # names, below someone named by their own.
            found_by = understand.PERSON if part.kind == understand.PERSON else understand.PLACE
            field = dict.fromkeys(known, rank.FIELD[found_by])
            if part.kind == understand.PERSON and part.own_ids and set(part.own_ids) != set(part.person_ids):
                own = set(understand.clips_with(session, part.own_ids, library_ids))
                field = {aid: f if aid in own else rank.FIELD["nickname"] for aid, f in field.items()}
            matched = {aid: rank.Rank(field=max(field.get(aid, 0),
                                                by_words[aid].field if aid in by_words else 0),
                                      quality=by_words[aid].quality if aid in by_words else 0.0)
                       for aid in {*known, *by_words}}
            # What found a clip by who's in it or where it was, when its words didn't say (they come first).
            for aid, by in known.items():
                if aid not in said:
                    said[aid] = SearchContext(score=0.0, matched_in=found_by, snippet=by)
            if found is None:
                found = matched
            elif u.rest:  # the words left rank what matches
                found = {aid: r for aid, r in found.items() if aid in matched}
            else:
                found = {aid: r.plus(matched[aid]) for aid, r in found.items() if aid in matched}
            for aid, c in said.items():
                contexts.setdefault(aid, c)
        for part in u.parts:
            if part.kind == understand.KIND:
                kept = set(understand.of_kind(session, list(found or {}), part.media, library_ids))
                found = {aid: r for aid, r in (found or {}).items() if aid in kept}
        found = found or {}
        ranks = found if ranks is None else {aid: r.plus(found[aid]) for aid, r in ranks.items() if aid in found}
    ranks = ranks or {}
    return ranks, {aid: c for aid, c in contexts.items() if aid in ranks}, "postgres"


def _words_search(session: Session, terms: list[str], library_ids: list[str] | None, limit: int,
                  public_cap_ms: int | None, public: bool, capped: set[str] | None,
                  also: Sequence[Mapping[str, Sequence[str]]] | None = None) -> tuple[dict, dict[str, SearchContext], str]:
    """The clips a search's words find (_run_text_search's, as it always was), each with its rank
    (src/server/search/rank.py: how much of the search it matches, rare words weighing more, where and how well);
    also: other words matching as each term's words do, by word (text_search.build)."""
    from src.server.search import rank, text_search

    try:
        q = text_search.build(terms, also)
    except text_search.SearchTooLongError as exc:  # said as the search box shows it
        raise ApiError(400, "search_too_long", str(exc),
                       {"max_words": text_search.MAX_WORDS, "max_searches": text_search.MAX_TERMS}) from None
    if q is None:
        return {}, {}, "none"

    scores: dict[str, float] = {}
    contexts: dict[str, SearchContext] = {}
    words: dict[str, list[bool]] = {}  # which of the search's words each clip matched, anywhere
    field: dict[str, int] = {}  # where it matched, the most telling (rank.FIELD)
    described: set[str] = set()  # matched in its description (a person's, if they wrote it)

    def full(name: str, hits: list, cap: int) -> list:
        if capped is not None and len(hits) >= cap:
            capped.add(name)
        return hits

    def seen(hit) -> None:
        had = words.setdefault(hit.asset_id, [False] * len(q.words))
        for i, matched in enumerate(hit.words[:len(had)]):
            had[i] = had[i] or matched
        field[hit.asset_id] = max(field.get(hit.asset_id, 0), rank.FIELD.get(hit.matched_in or "", rank.SPLIT))
        if hit.matched_in == "description":
            described.add(hit.asset_id)

    for hit in full("assets", text_search.clips(session, q, library_ids, limit, public=public,
                                                public_cap_ms=public_cap_ms), limit):
        seen(hit)
        if hit.asset_id not in scores or hit.score > scores[hit.asset_id]:
            scores[hit.asset_id] = hit.score
            contexts[hit.asset_id] = SearchContext(score=hit.score, hit_type="asset", snippet=hit.snippet,
                                                   matched_in=hit.matched_in)
    if not public and public_cap_ms is None:
        from src.server.repository.plugin_outputs import searched_plugins

        # A plug-in's facts are the clip's own words too, shown as what matched when they score higher.
        plugins = searched_plugins(session)
        for hit in full("facts", text_search.facts(session, q, library_ids, limit,
                                                   {pid: where for pid, (where, _) in plugins.items()}), limit):
            seen(hit)
            if hit.asset_id not in scores or hit.score > scores[hit.asset_id]:
                scores[hit.asset_id] = hit.score
                contexts[hit.asset_id] = SearchContext(score=hit.score, hit_type="asset", snippet=hit.snippet,
                                                       matched_in=hit.matched_in,
                                                       from_plugin=plugins[hit.source][1] if hit.source else None)

    # A scene or transcript segment: kept for its richer context (where in
    # the video) even when the clip's own words scored higher.
    for kind, hits in (("scene", full("scenes", text_search.scenes(session, q, library_ids, limit,
                                                                   public_cap_ms=public_cap_ms), limit)),
                       ("transcript", full("transcripts", text_search.segments(
                           session, q, library_ids, limit * 3, public_cap_ms=public_cap_ms), limit * 3))):
        for hit in hits:
            seen(hit)
            aid = hit.asset_id
            ctx = SearchContext(score=hit.score, hit_type=kind, snippet=hit.snippet,
                                start_ms=hit.start_ms, end_ms=hit.end_ms, matched_in=hit.matched_in)
            if aid not in scores or hit.score > scores[aid]:
                scores[aid] = hit.score
                contexts[aid] = ctx
            elif contexts[aid].hit_type == "asset":
                contexts[aid] = ctx.model_copy(update={"score": scores[aid]})

    for aid in rank.corrected(session, sorted(described)):  # a description a person wrote is their words
        field[aid] = max(field[aid], rank.FIELD["corrected"])
    covered = rank.coverage(words, len(q.words))
    ranks = {aid: rank.Rank(covered=covered[aid], field=field[aid], quality=score) for aid, score in scores.items()}
    return ranks, contexts, "postgres"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


_CAMERA_FILTERS = (CameraMake, CameraModel, LensModel, IsoRange, ApertureRange, FocalLengthRange,
                   ExposureRange, HasExposure)


def require_public_folder(session: Session, library_ids, path: str) -> None:
    """A visitor may narrow to a folder (folders are public, /directories
    lists them) but not to a file: probing names would find them."""
    from src.server.repository.assets import AssetRepository

    if not AssetRepository(session).folder_has_clips(list(library_ids), path):
        raise HTTPException(status_code=403, detail="Not a folder")


def guard_public_spec(request: Request, session: Session, spec) -> int | None:
    """For a public page's request, keep it to what the page shows.

    The middleware authorized one public library (the first library:
    filter). Require that every library in the filter is public, and
    refuse what a visitor has no business filtering by: ratings are a
    signed-in person's, and who's in a photo isn't for visitors to probe.
    Returns the public playback cap in ms (None if whole videos play, or
    the request isn't public): text search stays within it.
    """
    if not getattr(request.state, "is_public_request", False):
        return None
    # A plain AND only: an OR (at any depth) could widen scope past the library filter.
    root = spec.root
    if root.combinator != Combinator.AND or any(isinstance(c, GroupFilter) for c in root.children):
        raise HTTPException(status_code=403, detail="Grouped filters aren't available on public pages")
    if spec.needs_rating_join or any(isinstance(leaf, PersonFilter) for leaf in spec.leaves):
        raise HTTPException(status_code=403, detail="That filter isn't available on public pages")
    # Nor where a clip was shot: repeated near searches would find it (any
    # location: the file's, a person's, a guess; ADR-017).
    if any(isinstance(leaf, LOCATION_FILTERS) for leaf in spec.leaves):
        raise HTTPException(status_code=403, detail="Location filters aren't available on public pages")
    # Nor camera or exposure: the visitor view hides them, so filters mustn't reveal them.
    if any(isinstance(leaf, _CAMERA_FILTERS) for leaf in spec.leaves):
        raise HTTPException(status_code=403, detail="Camera filters aren't available on public pages")
    if spec.sort not in PUBLIC_SORTS:
        raise HTTPException(status_code=403, detail="That sort isn't available on public pages")
    scoped_lib_ids: set[str] = set()
    for leaf in spec.leaves:
        if isinstance(leaf, LibraryScope):
            scoped_lib_ids.update(leaf.library_ids)
    if not scoped_lib_ids:
        raise HTTPException(status_code=403, detail="Public access requires library scope")
    lib_repo = LibraryRepository(session)
    for lid in scoped_lib_ids:
        lib = lib_repo.get_by_id(lid)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")
    for leaf in spec.leaves:
        if isinstance(leaf, PathPrefix):
            require_public_folder(session, scoped_lib_ids, leaf.path)
    from src.server.tenant_settings import playback_cap

    cap = playback_cap(session, public=True)
    return None if cap is None else cap * 1000

def in_sight_spec(request: Request, session: Session, spec: QuerySpec) -> tuple[QuerySpec, list[str] | None]:
    """The query kept to the libraries the caller can see (ADR-018 decision
    8, roles per library), and the libraries its text search looks in: its
    first library filter's, those the caller can see among them; None for
    every library."""
    library_ids: list[str] | None = None
    for leaf in spec.leaves:
        if isinstance(leaf, LibraryScope):
            library_ids = list(leaf.library_ids)
            break
    visible = seen(request, session)
    if visible is None:
        return spec, library_ids
    kept = QuerySpec(root=GroupFilter(Combinator.AND, (LibraryScope(tuple(visible)), spec.root)),
                     sort=spec.sort, direction=spec.direction)
    return kept, visible if library_ids is None else [lib for lib in library_ids if lib in set(visible)]


def _visitor_view(item: QueryItem) -> QueryItem:
    """A result as a visitor to a public library sees it: what a public
    project's visitor sees of a clip (assets._project_visitor_view), not
    where it lives, where it was shot or what shot it."""
    return QueryItem(
        asset_id=item.asset_id,
        library_id=item.library_id,  # the public library the visitor asked for
        library_name=item.library_name,
        rel_path="",
        file_size=0,
        media_type=item.media_type,
        width=item.width,
        height=item.height,
        taken_at=item.taken_at,
        status=item.status,
        duration_sec=item.duration_sec,
        search_context=item.search_context,
    )


@router.get("", response_model=QueryResponse, dependencies=[Depends(anonymous_search_limited)])
def unified_query(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str | None, Depends(get_optional_user_id)],
    f: Annotated[list[str], Query(alias="f")] = [],  # noqa: B006
    sort: str = "taken_at",
    dir: str = "desc",
    after: str | None = None,
    limit: int = Query(default=200, ge=1, le=MAX_PAGE),
    best: int | None = Query(default=None, ge=1, le=MAX_BEST),
) -> QueryResponse:
    """Unified query endpoint — all filters via ?f=prefix:value params.

    When a ``f=query:...`` filter is present, the text search finds the
    candidates (candidate-set pattern) and the structured filters are applied
    to them, in relevance order. When no text search filter is present, runs
    pure SQL browse. `best` (a search's): its results follow `sort` instead,
    and its first page carries that many of its best matches by relevance,
    and how many there are (a page shows them apart, the rest by date)."""
    spec = parsed_filters(f, sort=sort, direction=dir)

    # --- Public-request guard ---
    # Public (unauthenticated) requests are authorized for exactly one
    # public library by the middleware. Enforce here that the LibraryScope
    # filter contains exactly that library and no others — otherwise an
    # attacker could pass `f=library:<public>&f=library:<private>` and
    # the middleware (which only inspects the first library: param)
    # would authorize the request for the public library while the
    # query handler returned content from the private one too.
    #
    # Visitors have no user: ratings are someone's, and who's in a photo
    # isn't for them to probe, so those filters are refused.
    public_cap_ms = guard_public_spec(request, session, spec)
    guard_project_filters(request, session, spec)
    spec, library_ids = in_sight_spec(request, session, spec)

    search_terms = spec.search_terms
    candidate_ids: list[str] | None = None
    candidate_scores: dict[str, float] | None = None
    search_contexts: dict[str, SearchContext] = {}
    total_estimate: int | None = None
    search_source: str | None = None

    # --- Text search via candidate-set pattern ---
    said: list[dict] = []
    understood: list[UnderstoodPart] | None = None
    best_ids: list[str] = []
    if search_terms:
        scores, contexts, search_source = _run_text_search(
            session, search_terms, library_ids, limit=MAX_CANDIDATE_IDS, public_cap_ms=public_cap_ms,
            public=getattr(request.state, "is_public_request", False), understood=said,
            assist_for=assisting(request, session, spec), best=best_ids,
        )
        understood = [UnderstoodPart(**part) for part in said] or None

        if not scores:
            return QueryResponse(
                items=[], total_estimate=0, search_source=search_source, understood=understood,
            )

        # Cap candidate set
        if len(scores) > MAX_CANDIDATE_IDS:
            top_ids = sorted(scores, key=scores.get, reverse=True)[:MAX_CANDIDATE_IDS]  # type: ignore[arg-type]
            scores = {aid: scores[aid] for aid in top_ids}
            contexts = {aid: contexts[aid] for aid in top_ids if aid in contexts}

        candidate_ids = list(scores.keys())
        candidate_scores = scores
        search_contexts = contexts
        total_estimate = len(scores)

    # --- Run query through repository ---
    browse_repo = UnifiedBrowseRepository(session)
    rating_user = user_id if spec.needs_rating_join else None
    best_matches: BestMatches | None = None
    if best is not None and candidate_scores is not None:
        if after is None:
            # The best matches with the page's filters, by relevance: the first of them, and how many.
            kept = browse_repo.page_ids(spec=spec, candidate_ids=[aid for aid in best_ids if aid in candidate_scores],
                                        candidate_scores=candidate_scores, rating_user_id=rating_user,
                                        limit=MAX_CANDIDATE_IDS)
            best_matches = BestMatches(items=_items(request, session, browse_repo, browse_repo.in_order(kept[:best]),
                                                    search_contexts), total=len(kept))
        candidate_scores = None  # the rest by the page's sort
    assets = browse_repo.query_page(
        spec=spec,
        candidate_ids=candidate_ids,
        candidate_scores=candidate_scores,
        rating_user_id=rating_user,
        after=after,
        limit=limit,
    )
    items = _items(request, session, browse_repo, assets, search_contexts)

    sort_col = spec.sort if spec.sort in SORT_COLUMNS else "taken_at"
    changed = browse_repo.changed_at_of([assets[-1].asset_id]) if assets and sort_col == "changed_at" else {}
    next_cursor: str | None = None
    if len(assets) == limit:
        last = assets[-1]
        sort_value = changed.get(last.asset_id) if sort_col == "changed_at" else getattr(last, sort_col, None)
        if sort_value is not None and hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)

    return QueryResponse(
        items=items,
        next_cursor=next_cursor,
        total_estimate=total_estimate,
        search_source=search_source,
        understood=understood,
        best=best_matches,
    )


def _items(request: Request, session: Session, browse_repo: UnifiedBrowseRepository, assets: list,
           search_contexts: dict[str, SearchContext]) -> list[QueryItem]:
    """The clips as a query answers them (a public library's visitor, as they see them)."""
    lib_repo = LibraryRepository(session)
    libs_by_id: dict[str, str] = {}
    for lid in {a.library_id for a in assets}:
        lib = lib_repo.get_by_id(lid)
        if lib:
            libs_by_id[lid] = lib.name
    changed = browse_repo.changed_at_of([a.asset_id for a in assets])

    items = [
        QueryItem(
            asset_id=a.asset_id,
            library_id=a.library_id,
            library_name=libs_by_id.get(a.library_id, ""),
            rel_path=a.rel_path,
            file_size=a.file_size,
            media_type=a.media_type,
            width=a.width,
            height=a.height,
            taken_at=a.taken_at.isoformat() if a.taken_at else None,
            status=a.status,
            duration_sec=a.duration_sec,
            camera_make=a.camera_make,
            camera_model=a.camera_model,
            iso=a.iso,
            aperture=a.aperture,
            focal_length=a.focal_length,
            focal_length_35mm=a.focal_length_35mm,
            lens_model=a.lens_model,
            flash_fired=a.flash_fired,
            gps_lat=a.gps_lat,
            gps_lon=a.gps_lon,
            face_count=a.face_count,
            thumbnail_key=a.thumbnail_key,
            proxy_key=a.proxy_key,
            created_at=a.created_at.isoformat() if a.created_at else None,
            changed_at=changed[a.asset_id].isoformat() if changed.get(a.asset_id) else None,
            search_context=search_contexts.get(a.asset_id),
        )
        for a in assets
    ]

    if getattr(request.state, "is_public_request", False):
        items = [_visitor_view(i) for i in items]
    return items
