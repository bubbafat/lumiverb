"""Facets endpoint: aggregated filter values for the current library view."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.shared.io_utils import normalize_path_prefix
from src.server.repository.tenant import LibraryRepository

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
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    path_prefix: str | None = None,
) -> FacetsResponse:
    """Return aggregated filter values for the current library view."""
    lib = LibraryRepository(session).get_by_id(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="Library not found")

    conditions = ["library_id = :library_id"]
    params: dict[str, object] = {"library_id": library_id}

    if path_prefix:
        normalized = normalize_path_prefix(path_prefix)
        if normalized and ".." in normalized.split("/"):
            raise HTTPException(status_code=400, detail="Invalid path_prefix")
        if normalized:
            conditions.append(
                "(rel_path = :path_prefix OR rel_path LIKE :path_prefix_like)"
            )
            params["path_prefix"] = normalized
            params["path_prefix_like"] = normalized + "/%"

    where_sql = " AND ".join(conditions)

    sql = f"""
        SELECT
            array_agg(DISTINCT camera_make) FILTER (WHERE camera_make IS NOT NULL) AS camera_makes,
            array_agg(DISTINCT camera_model) FILTER (WHERE camera_model IS NOT NULL) AS camera_models,
            array_agg(DISTINCT lens_model) FILTER (WHERE lens_model IS NOT NULL) AS lens_models,
            MIN(iso) AS iso_min,
            MAX(iso) AS iso_max,
            MIN(aperture) AS aperture_min,
            MAX(aperture) AS aperture_max,
            MIN(focal_length) AS fl_min,
            MAX(focal_length) AS fl_max,
            bool_or(media_type = 'image') AS has_images,
            bool_or(media_type = 'video') AS has_videos,
            COUNT(*) FILTER (WHERE gps_lat IS NOT NULL AND gps_lon IS NOT NULL) AS gps_count,
            COUNT(*) FILTER (WHERE face_count > 0) AS face_count
        FROM active_assets
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
