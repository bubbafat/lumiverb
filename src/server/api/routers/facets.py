"""Facets endpoint: aggregated filter values, respecting active filters.

Accepts the same ``?f=prefix:value`` params as ``/v1/query`` so that facet
counts reflect the currently active filter set.  When a text search filter
is present, the clips it finds (src/server/search/text_search.py) scope the
aggregation.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.models.filter_registry import parse_f_params
from src.server.models.query_filter import LibraryScope

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
    has_gps_count: int = 0  # clips with a location: the file's or a person's (guesses with include_guesses)
    has_face_count: int = 0
    # Clips with an applied guess (0 until the location producer makes some):
    # whether the "Includes guesses" toggle is worth showing.
    guess_count: int = 0


@router.get("/facets", response_model=FacetsResponse)
def get_facets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    f: Annotated[list[str], Query(alias="f")] = [],  # noqa: B006
) -> FacetsResponse:
    """Return aggregated filter values, scoped by the active filter set.

    Accepts the same ``?f=prefix:value`` params as ``/v1/query``.
    """
    from src.server.api.routers.projects import guard_project_filters
    from src.server.api.routers.query import guard_public_spec

    spec = parse_f_params(f)
    public_cap_ms = guard_public_spec(request, session, spec)
    guard_project_filters(request, session, spec)

    # --- Candidate set from text search ---
    candidate_ids: list[str] | None = None
    if spec.search_terms:
        from src.server.api.routers.query import MAX_CANDIDATE_IDS, _run_text_search

        library_ids: list[str] | None = None
        for leaf in spec.leaves:
            if isinstance(leaf, LibraryScope):
                library_ids = list(leaf.library_ids)
                break
        scores, _contexts, _source = _run_text_search(
            session, spec.search_terms, library_ids, limit=MAX_CANDIDATE_IDS, public_cap_ms=public_cap_ms,
            public=getattr(request.state, "is_public_request", False),
        )
        if not scores:
            return _empty_facets()
        candidate_ids = list(scores.keys())

    from src.server.repository.browse import UnifiedBrowseRepository

    row = UnifiedBrowseRepository(session).facets(spec, candidate_ids)

    media_types: list[str] = []
    if row.has_images:
        media_types.append("image")
    if row.has_videos:
        media_types.append("video")

    if getattr(request.state, "is_public_request", False):
        # A public page's visitor sees no location or camera details (as a
        # public project's doesn't): not counted here either.
        return _empty_facets().model_copy(update={
            "media_types": media_types, "has_face_count": row.face_count or 0})

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
        guess_count=row.guess_count or 0,
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
