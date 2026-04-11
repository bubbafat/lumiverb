"""Facets endpoint: aggregated filter values, respecting active filters.

Accepts the same ``?f=prefix:value`` params as ``/v1/query`` so that facet
counts reflect the currently active filter set.  When a text search filter
is present, Quickwit candidate IDs scope the aggregation.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.models.filter_registry import parse_f_params
from src.server.models.query_filter import LeafFilter, LibraryScope, SearchTerm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["assets"])


class FacetsResponse(BaseModel):
    media_types: list[str]
    camera_makes: list[str]
    camera_models: list[str]
    lens_models: list[str]
    iso_range: list[int | None]  # [min, max]
    aperture_range: list[float | None]  # [min, max]
    focal_length_range: list[float | None]  # [min, max]
    has_gps_count: int = 0
    has_face_count: int = 0


@router.get("/facets", response_model=FacetsResponse)
def get_facets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    f: Annotated[list[str], Query(alias="f")] = [],  # noqa: B006
) -> FacetsResponse:
    """Return aggregated filter values, scoped by the active filter set.

    Accepts the same ``?f=prefix:value`` params as ``/v1/query``.
    """
    spec = parse_f_params(f)

    conditions: list[str] = []
    params: dict[str, object] = {}
    counter = [0]

    # --- Candidate set from text search ---
    candidate_ids: list[str] | None = None
    if spec.search_terms:
        tenant_id = getattr(request.state, "tenant_id", None)
        library_ids: list[str] | None = None
        for leaf in spec.leaves:
            if isinstance(leaf, LibraryScope):
                library_ids = list(leaf.library_ids)
                break
        if tenant_id:
            from src.server.api.routers.query import (
                MAX_CANDIDATE_IDS,
                _run_postgres_fallback,
                _run_quickwit_search,
            )

            scores, contexts, source = _run_quickwit_search(
                tenant_id, spec.search_terms, library_ids, limit=MAX_CANDIDATE_IDS,
            )
            if source == "postgres_fallback":
                combined_query = " AND ".join(f"({st.q})" for st in spec.search_terms if st.q)
                scores, contexts = _run_postgres_fallback(
                    session, combined_query, library_ids, limit=MAX_CANDIDATE_IDS,
                )
            if not scores:
                return _empty_facets()
            candidate_ids = list(scores.keys())

    if candidate_ids is not None:
        conditions.append("a.asset_id = ANY(:candidate_ids)")
        params["candidate_ids"] = candidate_ids

    # --- Structured filter SQL conditions ---
    joins: list[str] = []
    needs_rating = False
    needs_metadata = False

    for leaf in spec.leaves:
        if isinstance(leaf, SearchTerm):
            continue  # handled via candidate set above
        sql_frag = leaf.to_sql(params, counter)
        conditions.append(sql_frag)
        if leaf.needs_rating_join:
            needs_rating = True
        if leaf.needs_metadata_join:
            needs_metadata = True

    # Build FROM clause with necessary JOINs
    from_clause = "active_assets a"
    if needs_rating:
        joins.append(
            "LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id"
        )
    if needs_metadata:
        joins.append(
            "LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id"
        )

    where_sql = " AND ".join(conditions) if conditions else "TRUE"
    join_sql = " ".join(joins)

    sql = f"""
        SELECT
            array_agg(DISTINCT a.camera_make) FILTER (WHERE a.camera_make IS NOT NULL) AS camera_makes,
            array_agg(DISTINCT a.camera_model) FILTER (WHERE a.camera_model IS NOT NULL) AS camera_models,
            array_agg(DISTINCT a.lens_model) FILTER (WHERE a.lens_model IS NOT NULL) AS lens_models,
            MIN(a.iso) AS iso_min,
            MAX(a.iso) AS iso_max,
            MIN(a.aperture) AS aperture_min,
            MAX(a.aperture) AS aperture_max,
            MIN(a.focal_length) AS fl_min,
            MAX(a.focal_length) AS fl_max,
            bool_or(a.media_type = 'image') AS has_images,
            bool_or(a.media_type = 'video') AS has_videos,
            COUNT(*) FILTER (WHERE a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL) AS gps_count,
            COUNT(*) FILTER (WHERE a.face_count > 0) AS face_count
        FROM {from_clause}
        {join_sql}
        WHERE {where_sql}
    """

    row = session.execute(text(sql).bindparams(**params)).one()

    media_types: list[str] = []
    if row.has_images:
        media_types.append("image")
    if row.has_videos:
        media_types.append("video")

    return FacetsResponse(
        media_types=media_types,
        camera_makes=sorted(row.camera_makes or []),
        camera_models=sorted(row.camera_models or []),
        lens_models=sorted(row.lens_models or []),
        iso_range=[row.iso_min, row.iso_max],
        aperture_range=[row.aperture_min, row.aperture_max],
        focal_length_range=[row.fl_min, row.fl_max],
        has_gps_count=row.gps_count or 0,
        has_face_count=row.face_count or 0,
    )


def _empty_facets() -> FacetsResponse:
    return FacetsResponse(
        media_types=[],
        camera_makes=[],
        camera_models=[],
        lens_models=[],
        iso_range=[None, None],
        aperture_range=[None, None],
        focal_length_range=[None, None],
        has_gps_count=0,
        has_face_count=0,
    )
