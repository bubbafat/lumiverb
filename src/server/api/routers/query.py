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
    # Diagnostic: which search backend produced the candidate set —
    # "quickwit" (BM25), "postgres_fallback" (ILIKE substring), or
    # "none" (no text search). Lets clients (and operators) tell at a
    # glance whether the BM25 index is engaged. None when no text
    # search was run at all.
    search_source: str | None = None
    # Temporary: returns the constructed Quickwit query, raw hit
    # counts per index, and any exception text. Lets us debug
    # "search_source=postgres_fallback" without SSH access to logs.
    # Remove once the underlying issue is fixed.
    diag: dict | None = None


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
    diag: dict | None = None,
) -> tuple[dict[str, float], dict[str, SearchContext], str]:
    """Run text search through Quickwit (or Postgres fallback).

    Returns:
      - scores: {asset_id: best_score}
      - contexts: {asset_id: SearchContext} (scene/transcript annotations)
      - source: "quickwit" | "postgres"

    If `diag` is provided, populates it with query string, hit counts,
    and any exception text — for surfacing to clients during debug.
    """
    from src.server.search.query_builder import build_quickwit_query

    # Build per-term field-boosted clauses, then AND the terms together
    # at the top level. This is the difference between "underperforming"
    # search and search that ranks descriptive matches above incidental
    # OCR / path noise — see query_builder for the rationale.
    per_term = [build_quickwit_query(st.q) for st in search_terms if st.q]
    per_term = [q for q in per_term if q]
    if not per_term:
        return {}, {}, "none"
    combined_query = " AND ".join(f"({q})" for q in per_term)
    if diag is not None:
        diag["quickwit_query"] = combined_query
        diag["library_ids"] = library_ids

    scores: dict[str, float] = {}
    contexts: dict[str, SearchContext] = {}

    from src.server.config import get_settings
    settings = get_settings()

    try:
        from src.server.search.quickwit_client import QuickwitClient
        qw = QuickwitClient()
        if diag is not None:
            diag["quickwit_enabled"] = qw.enabled
            diag["quickwit_base_url"] = getattr(qw, "_base_url", None)

        if qw.enabled:
            # Asset index
            asset_hits = qw.search_tenant(
                tenant_id=tenant_id,
                query=combined_query,
                library_ids=library_ids,
                max_hits=limit,
            )
            if diag is not None:
                diag["asset_hits"] = len(asset_hits)
                if asset_hits:
                    diag["asset_top_score"] = asset_hits[0].get("score")
            for hit in asset_hits:
                aid = hit["asset_id"]
                score = hit.get("score", 0.0)
                if aid not in scores or score > scores[aid]:
                    scores[aid] = score
                    contexts[aid] = SearchContext(score=score, hit_type="asset")

            # Scene index
            scene_hits = qw.search_tenant_scenes(
                tenant_id=tenant_id,
                query=combined_query,
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

            # Transcript index
            transcript_hits = qw.search_tenant_transcripts(
                tenant_id=tenant_id,
                query=combined_query,
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

            # If Quickwit returned results, use them
            if scores:
                return scores, contexts, "quickwit"

            # Quickwit returned empty — fall back to postgres if configured
            if settings.quickwit_fallback_to_postgres:
                return scores, contexts, "postgres_fallback"

            return scores, contexts, "quickwit"

    except Exception as exc:
        if diag is not None:
            diag["quickwit_exception"] = repr(exc)
        logger.warning("Quickwit raised %r — falling back to Postgres", exc, exc_info=True)

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
    diag: dict | None = {} if search_terms else None

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
            diag=diag,
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
                items=[], total_estimate=0, search_source=search_source, diag=diag,
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
        diag=diag,
    )
