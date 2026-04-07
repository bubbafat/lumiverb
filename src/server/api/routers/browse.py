"""Unified cross-library browse endpoint."""

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session
from src.shared.io_utils import normalize_path_prefix
from src.server.repository.tenant import LibraryRepository, UnifiedBrowseRepository

router = APIRouter(prefix="/v1/browse", tags=["browse"])


class BrowseItem(BaseModel):
    asset_id: str
    library_id: str
    library_name: str
    rel_path: str
    file_size: int
    file_mtime: str | None
    sha256: str | None
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
    created_at: str | None = None


class BrowseResponse(BaseModel):
    items: list[BrowseItem]
    next_cursor: str | None = None


SORT_COLUMNS = {"taken_at", "created_at", "file_size", "iso", "exposure_time_us", "aperture", "focal_length", "rel_path", "asset_id"}


def _encode_cursor(sort_col: str, sort_value: object, asset_id: str) -> str:
    payload = json.dumps({"v": sort_value, "id": asset_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


@router.get("", response_model=BrowseResponse)
def browse_assets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    after: str | None = None,
    limit: int = 500,
    library_id: str | None = None,
    path_prefix: str | None = None,
    tag: str | None = None,
    sort: str = "taken_at",
    dir: str = "desc",
    media_type: str | None = None,
    camera_make: str | None = None,
    camera_model: str | None = None,
    lens_model: str | None = None,
    iso_min: int | None = None,
    iso_max: int | None = None,
    exposure_min_us: int | None = None,
    exposure_max_us: int | None = None,
    aperture_min: float | None = None,
    aperture_max: float | None = None,
    focal_length_min: float | None = None,
    focal_length_max: float | None = None,
    has_exposure: bool | None = None,
    has_gps: bool = False,
    near_lat: float | None = None,
    near_lon: float | None = None,
    near_radius_km: float = 1.0,
    favorite: bool | None = None,
    star_min: int | None = None,
    star_max: int | None = None,
    color: str | None = None,
    has_rating: bool | None = None,
    has_faces: bool | None = None,
    person_id: str | None = None,
) -> BrowseResponse:
    """Cross-library paginated browse with full filter support."""
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1

    # path_prefix requires library_id
    if path_prefix and not library_id:
        raise HTTPException(
            status_code=400,
            detail="path_prefix requires library_id (paths are library-relative)",
        )

    sort_col = sort if sort in SORT_COLUMNS else "taken_at"
    direction = dir if dir in ("asc", "desc") else "desc"

    normalized_prefix: str | None = None
    if path_prefix is not None:
        normalized_prefix = normalize_path_prefix(path_prefix)
        if normalized_prefix and ".." in normalized_prefix.split("/"):
            raise HTTPException(
                status_code=400,
                detail="Invalid path_prefix; path traversal not allowed",
            )

    library_ids: list[str] | None = None
    if library_id:
        library_ids = [lid.strip() for lid in library_id.split(",") if lid.strip()]

    media_types: list[str] | None = None
    if media_type:
        media_types = [m.strip() for m in media_type.split(",") if m.strip()]

    # Rating filters use current user identity
    rating_user_id: str | None = None
    color_list: list[str] | None = None
    needs_rating_filter = favorite is not None or star_min is not None or star_max is not None or color is not None or has_rating is not None
    if needs_rating_filter:
        rating_user_id = user_id
        if color is not None:
            color_list = [c.strip() for c in color.split(",") if c.strip()]

    browse_repo = UnifiedBrowseRepository(session)
    assets = browse_repo.page(
        after=after,
        limit=limit,
        library_ids=library_ids,
        path_prefix=normalized_prefix,
        sort=sort_col,
        direction=direction,
        media_types=media_types,
        camera_make=camera_make,
        camera_model=camera_model,
        lens_model=lens_model,
        iso_min=iso_min,
        iso_max=iso_max,
        exposure_min_us=exposure_min_us,
        exposure_max_us=exposure_max_us,
        aperture_min=aperture_min,
        aperture_max=aperture_max,
        focal_length_min=focal_length_min,
        focal_length_max=focal_length_max,
        has_exposure=has_exposure,
        has_gps=has_gps,
        near_lat=near_lat,
        near_lon=near_lon,
        near_radius_km=near_radius_km,
        rating_user_id=rating_user_id,
        favorite=favorite,
        star_min=star_min,
        star_max=star_max,
        color=color_list,
        has_rating=has_rating,
        has_faces=has_faces,
        person_id=person_id,
    )

    # Resolve library names
    lib_repo = LibraryRepository(session)
    lib_ids = list({a.library_id for a in assets})
    libs_by_id: dict[str, str] = {}
    for lid in lib_ids:
        lib = lib_repo.get_by_id(lid)
        if lib:
            libs_by_id[lid] = lib.name

    items = [
        BrowseItem(
            asset_id=a.asset_id,
            library_id=a.library_id,
            library_name=libs_by_id.get(a.library_id, ""),
            rel_path=a.rel_path,
            file_size=a.file_size,
            file_mtime=a.file_mtime.isoformat() if a.file_mtime else None,
            sha256=a.sha256,
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
            created_at=a.created_at.isoformat() if a.created_at else None,
        )
        for a in assets
    ]

    next_cursor: str | None = None
    if items and len(items) == limit:
        last = assets[-1]
        sort_value = getattr(last, sort_col, None)
        if hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)

    return BrowseResponse(items=items, next_cursor=next_cursor)
