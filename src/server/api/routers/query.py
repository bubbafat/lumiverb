"""Unified query endpoint — replaces /v1/browse and /v1/search.

Accepts filter algebra via repeated ?f=prefix:value params. When a SearchTerm
filter is present, uses the candidate-set pattern: Quickwit returns candidate
IDs + scores, then Postgres applies all structured filters and pagination.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session
from src.server.models.filter_registry import parse_f_params
from src.server.models.query_filter import LibraryScope, SearchTerm
from src.server.repository.tenant import LibraryRepository, UnifiedBrowseRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/query", tags=["query"])

SORT_COLUMNS = {
    "taken_at", "created_at", "file_size", "iso",
    "exposure_time_us", "aperture", "focal_length", "rel_path", "asset_id",
}

MAX_CANDIDATE_IDS = 5000


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class SearchContext(BaseModel):
    score: float
    hit_type: str = "asset"
    snippet: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


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
    search_context: SearchContext | None = None


class QueryResponse(BaseModel):
    items: list[QueryItem]
    next_cursor: str | None = None
    total_estimate: int | None = None
    # Which search backend produced the candidate set — "quickwit"
    # (BM25), "postgres_fallback" (ILIKE substring), or "none" (no
    # text search). Lets clients (and operators) tell at a glance
    # whether the BM25 index is engaged. None when no text search
    # was run at all.
    search_source: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_cursor(sort_col: str, sort_value: object, asset_id: str) -> str:
    payload = json.dumps({"v": sort_value, "id": asset_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _run_quickwit_search(
    tenant_id: str,
    search_terms: list[SearchTerm],
    library_ids: list[str] | None,
    limit: int,
) -> tuple[dict[str, float], dict[str, SearchContext], str]:
    """Run text search through Quickwit (or Postgres fallback).

    Returns:
      - scores: {asset_id: best_score}
      - contexts: {asset_id: SearchContext} (scene/transcript annotations)
      - source: "quickwit" | "postgres"
    """
    from src.server.search.query_builder import (
        ASSET_FIELDS,
        ASSET_PHRASE_FIELDS,
        SCENE_FIELDS,
        SCENE_PHRASE_FIELDS,
        TRANSCRIPT_FIELDS,
        TRANSCRIPT_PHRASE_FIELDS,
        build_quickwit_prefix_query,
        build_quickwit_query,
    )

    # Quickwit's three indexes have *different* schemas — naming a
    # field the target index doesn't define makes Quickwit return 400
    # and the catch below silently drops the whole search into the
    # Postgres fallback. So we build a different query string per
    # index, each restricted to fields that index actually has.
    def _build(fields: list[str], phrase_fields: list[str]) -> str:
        per_term = [
            build_quickwit_query(st.q, fields=fields, phrase_fields=phrase_fields)
            for st in search_terms if st.q
        ]
        per_term = [q for q in per_term if q]
        if not per_term:
            return ""
        return " AND ".join(f"({q})" for q in per_term)

    def _build_prefix(fields: list[str]) -> str:
        per_term = [
            build_quickwit_prefix_query(st.q, fields=fields)
            for st in search_terms if st.q
        ]
        per_term = [q for q in per_term if q]
        if not per_term:
            return ""
        return " AND ".join(f"({q})" for q in per_term)

    asset_query = _build(ASSET_FIELDS, ASSET_PHRASE_FIELDS)
    scene_query = _build(SCENE_FIELDS, SCENE_PHRASE_FIELDS)
    transcript_query = _build(TRANSCRIPT_FIELDS, TRANSCRIPT_PHRASE_FIELDS)
    asset_prefix_query = _build_prefix(ASSET_FIELDS)
    scene_prefix_query = _build_prefix(SCENE_FIELDS)

    if not asset_query:
        return {}, {}, "none"

    scores: dict[str, float] = {}
    contexts: dict[str, SearchContext] = {}

    from src.server.config import get_settings
    settings = get_settings()

    try:
        from src.server.search.quickwit_client import QuickwitClient
        qw = QuickwitClient()
    except Exception as exc:
        logger.warning("Quickwit client init failed: %r", exc, exc_info=True)
        qw = None

    if qw is not None and qw.enabled:
        # Each per-index search is wrapped in its own try/except so a
        # failure on one index (e.g. transcript index doesn't exist for
        # this tenant) doesn't crash the whole search into the
        # Postgres fallback. Previously a 400 from the transcript
        # index would discard 1142 perfectly good asset hits.

        # Asset index — issue the EXACT query first, then the PREFIX
        # query separately. They MUST be separate Quickwit calls
        # because Quickwit's wildcard queries get constant scoring
        # rather than BM25; OR'ing them into a single query lets
        # exact matches dominate so completely that prefix-only
        # results (e.g. "Disney" → "Disneyland") get buried at
        # rank 100+. By scoring the prefix list independently with
        # position scoring and a penalty multiplier, prefix rank 0
        # ties with exact rank 1 and prefix-only matches surface
        # near the top of the results.
        try:
            asset_hits = qw.search_tenant(
                tenant_id=tenant_id,
                query=asset_query,
                library_ids=library_ids,
                max_hits=limit,
            )
            for hit in asset_hits:
                aid = hit["asset_id"]
                score = hit.get("score", 0.0)
                if aid not in scores or score > scores[aid]:
                    scores[aid] = score
                    # Surface the asset's AI description as the snippet
                    # so iOS cell captions and the lightbox Match
                    # section have something to show for photo hits.
                    # Falls back to joining the top tags when the
                    # description is missing.
                    snippet = hit.get("description") or ""
                    if not snippet:
                        tags = hit.get("tags") or []
                        if tags:
                            snippet = " · ".join(tags[:4])
                    contexts[aid] = SearchContext(
                        score=score,
                        hit_type="asset",
                        snippet=snippet or None,
                    )
        except Exception as exc:
            logger.warning("Quickwit asset search failed: %r", exc, exc_info=True)

        # Asset prefix expansion — separate call so prefix-only
        # matches get their own position-based scoring and aren't
        # buried by Quickwit's constant-scoring of wildcards.
        if asset_prefix_query:
            try:
                prefix_hits = qw.search_tenant(
                    tenant_id=tenant_id,
                    query=asset_prefix_query,
                    library_ids=library_ids,
                    max_hits=limit,
                )
                # Apply prefix penalty so a prefix rank-0 ties with
                # exact rank-1 (1/(1+1)=0.5). High-rank prefix matches
                # still surface above low-rank exact matches.
                PREFIX_PENALTY = 0.5
                for hit in prefix_hits:
                    aid = hit["asset_id"]
                    raw_score = hit.get("score", 0.0)
                    score = raw_score * PREFIX_PENALTY
                    if aid not in scores or score > scores[aid]:
                        scores[aid] = score
                        snippet = hit.get("description") or ""
                        if not snippet:
                            tags = hit.get("tags") or []
                            if tags:
                                snippet = " · ".join(tags[:4])
                        contexts[aid] = SearchContext(
                            score=score,
                            hit_type="asset",
                            snippet=snippet or None,
                        )
            except Exception as exc:
                logger.warning("Quickwit asset prefix search failed: %r", exc, exc_info=True)

        # Scene index — uses scene-restricted query (description, tags only)
        try:
            scene_hits = qw.search_tenant_scenes(
                tenant_id=tenant_id,
                query=scene_query,
                library_ids=library_ids,
                max_hits=limit,
            )
            for hit in scene_hits:
                aid = hit["asset_id"]
                score = hit.get("score", 0.0)
                ctx = SearchContext(
                    score=score,
                    hit_type="scene",
                    snippet=hit.get("description"),
                    start_ms=hit.get("start_ms"),
                    end_ms=hit.get("end_ms"),
                )
                # Prefer scene/transcript (richer context), keep higher score
                if aid not in scores:
                    scores[aid] = score
                    contexts[aid] = ctx
                elif score > scores[aid]:
                    scores[aid] = score
                    contexts[aid] = ctx
                elif contexts[aid].hit_type == "asset":
                    # Scene has richer context even at lower score
                    contexts[aid] = SearchContext(
                        score=scores[aid],
                        hit_type="scene",
                        snippet=ctx.snippet,
                        start_ms=ctx.start_ms,
                        end_ms=ctx.end_ms,
                    )
        except Exception as exc:
            logger.warning("Quickwit scene search failed: %r", exc, exc_info=True)

        # Scene prefix expansion — same pattern as the asset prefix
        # expansion above. Separate call so prefix matches get
        # position-based scoring and a penalty.
        if scene_prefix_query:
            try:
                scene_prefix_hits = qw.search_tenant_scenes(
                    tenant_id=tenant_id,
                    query=scene_prefix_query,
                    library_ids=library_ids,
                    max_hits=limit,
                )
                PREFIX_PENALTY = 0.5
                for hit in scene_prefix_hits:
                    aid = hit["asset_id"]
                    raw_score = hit.get("score", 0.0)
                    score = raw_score * PREFIX_PENALTY
                    ctx = SearchContext(
                        score=score,
                        hit_type="scene",
                        snippet=hit.get("description"),
                        start_ms=hit.get("start_ms"),
                        end_ms=hit.get("end_ms"),
                    )
                    if aid not in scores or score > scores[aid]:
                        scores[aid] = score
                        contexts[aid] = ctx
            except Exception as exc:
                logger.warning("Quickwit scene prefix search failed: %r", exc, exc_info=True)

        # Transcript index — uses transcript-restricted query (text only).
        # The transcript index is created lazily on first transcript
        # submission, so for libraries with no video transcripts the
        # index doesn't exist and Quickwit returns 400. We swallow that
        # specifically — no transcripts means no transcript hits, which
        # is fine. Other transcript failures still get logged.
        try:
            transcript_hits = qw.search_tenant_transcripts(
                tenant_id=tenant_id,
                query=transcript_query,
                library_ids=library_ids,
                max_hits=limit * 3,
            )
            # Deduplicate: keep best per asset
            for hit in transcript_hits:
                aid = hit["asset_id"]
                score = hit.get("score", 0.0)
                ctx = SearchContext(
                    score=score,
                    hit_type="transcript",
                    snippet=hit.get("text"),
                    start_ms=hit.get("start_ms"),
                    end_ms=hit.get("end_ms"),
                )
                if aid not in scores:
                    scores[aid] = score
                    contexts[aid] = ctx
                elif score > scores[aid]:
                    scores[aid] = score
                    contexts[aid] = ctx
                elif contexts[aid].hit_type == "asset":
                    contexts[aid] = SearchContext(
                        score=scores[aid],
                        hit_type="transcript",
                        snippet=ctx.snippet,
                        start_ms=ctx.start_ms,
                        end_ms=ctx.end_ms,
                    )
        except Exception as exc:
            logger.warning("Quickwit transcript search failed: %r", exc, exc_info=True)

        # If Quickwit (any index) returned results, use them
        if scores:
            return scores, contexts, "quickwit"

        # Quickwit returned empty across all indexes — fall back to
        # postgres if configured.
        if settings.quickwit_fallback_to_postgres:
            return scores, contexts, "postgres_fallback"

        return scores, contexts, "quickwit"

    # Postgres ILIKE fallback
    return scores, contexts, "postgres_fallback"


def _run_postgres_fallback(
    session: Session,
    combined_query: str,
    library_ids: list[str] | None,
    limit: int,
) -> tuple[dict[str, float], dict[str, SearchContext]]:
    """Postgres ILIKE fallback when Quickwit is unavailable."""
    from src.server.search.postgres_search import search_assets

    lib_id = library_ids[0] if library_ids and len(library_ids) == 1 else None
    hits = search_assets(session, lib_id, combined_query, limit=limit)
    scores: dict[str, float] = {}
    contexts: dict[str, SearchContext] = {}
    for hit in hits:
        aid = hit["asset_id"]
        scores[aid] = 0.0
        contexts[aid] = SearchContext(score=0.0, hit_type="asset")
    return scores, contexts


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("", response_model=QueryResponse)
def unified_query(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    f: Annotated[list[str], Query(alias="f")] = [],  # noqa: B006
    sort: str = "taken_at",
    dir: str = "desc",
    after: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
) -> QueryResponse:
    """Unified query endpoint — all filters via ?f=prefix:value params.

    When a ``f=query:...`` filter is present, text search runs through Quickwit
    (candidate-set pattern) with structured filters applied in Postgres. When no
    text search filter is present, runs pure SQL browse.
    """
    spec = parse_f_params(f, sort=sort, direction=dir)

    # --- Public-request guard ---
    # Public (unauthenticated) requests are authorized for exactly one
    # public library by the middleware. Enforce here that the LibraryScope
    # filter contains exactly that library and no others — otherwise an
    # attacker could pass `f=library:<public>&f=library:<private>` and
    # the middleware (which only inspects the first library: param)
    # would authorize the request for the public library while the
    # query handler returned content from the private one too.
    #
    # Today this is also blocked by `get_current_user_id` 401-ing public
    # requests, but that's a coincidental defense — this guard makes the
    # endpoint safe even if the user_id requirement is later relaxed for
    # anonymous public browsing.
    if getattr(request.state, "is_public_request", False):
        lib_repo = LibraryRepository(session)
        scoped_lib_ids: set[str] = set()
        for leaf in spec.leaves:
            if isinstance(leaf, LibraryScope):
                scoped_lib_ids.update(leaf.library_ids)
        if not scoped_lib_ids:
            raise HTTPException(status_code=403, detail="Public access requires library scope")
        for lid in scoped_lib_ids:
            lib = lib_repo.get_by_id(lid)
            if lib is None or not lib.is_public:
                raise HTTPException(status_code=404, detail="Not found")

    search_terms = spec.search_terms
    candidate_ids: list[str] | None = None
    candidate_scores: dict[str, float] | None = None
    search_contexts: dict[str, SearchContext] = {}
    total_estimate: int | None = None
    search_source: str | None = None

    # --- Text search via candidate-set pattern ---
    if search_terms:
        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            raise HTTPException(status_code=500, detail="Tenant context not available")

        # Extract library_ids from the filter tree for Quickwit scoping
        library_ids: list[str] | None = None
        for leaf in spec.leaves:
            if isinstance(leaf, LibraryScope):
                library_ids = list(leaf.library_ids)
                break

        scores, contexts, source = _run_quickwit_search(
            tenant_id, search_terms, library_ids, limit=MAX_CANDIDATE_IDS,
        )
        search_source = source

        if source == "postgres_fallback":
            # Join raw terms for ILIKE — no parentheses (those are Quickwit syntax)
            pg_query = " ".join(st.q for st in search_terms if st.q)
            scores, contexts = _run_postgres_fallback(
                session, pg_query, library_ids, limit=MAX_CANDIDATE_IDS,
            )

        if not scores:
            return QueryResponse(
                items=[], total_estimate=0, search_source=search_source,
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
    assets = browse_repo.query_page(
        spec=spec,
        candidate_ids=candidate_ids,
        candidate_scores=candidate_scores,
        rating_user_id=user_id if spec.needs_rating_join else None,
        after=after,
        limit=limit,
    )

    # --- Resolve library names ---
    lib_repo = LibraryRepository(session)
    lib_ids = list({a.library_id for a in assets})
    libs_by_id: dict[str, str] = {}
    for lid in lib_ids:
        lib = lib_repo.get_by_id(lid)
        if lib:
            libs_by_id[lid] = lib.name

    # --- Build response ---
    sort_col = spec.sort if spec.sort in SORT_COLUMNS else "taken_at"

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
            search_context=search_contexts.get(a.asset_id),
        )
        for a in assets
    ]

    next_cursor: str | None = None
    if len(assets) == limit:
        last = assets[-1]
        sort_value = getattr(last, sort_col, None)
        if sort_value is not None and hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)

    return QueryResponse(
        items=items,
        next_cursor=next_cursor,
        total_estimate=total_estimate,
        search_source=search_source,
    )
