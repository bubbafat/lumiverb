"""Asset ratings API: per-user favorites, stars, and color labels."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session
from src.server.models.tenant import VALID_COLORS
from src.server.repository.tenant import AssetRepository, LibraryRepository, RatingRepository, _SENTINEL

router = APIRouter(prefix="/v1/assets", tags=["ratings"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class RatingUpdate(BaseModel):
    favorite: bool | None = None
    stars: int | None = None
    color: str | None = None

    # Sentinel: distinguish "color not provided" from "color = null (clear)"
    _color_provided: bool = False

    def model_post_init(self, __context: object) -> None:
        # Track whether color was explicitly in the payload
        # (Pydantic sets it to None both for missing and explicit null)
        pass

    @field_validator("stars")
    @classmethod
    def validate_stars(cls, v: int | None) -> int | None:
        if v is not None and (v < 0 or v > 5):
            raise ValueError("stars must be between 0 and 5")
        return v

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_COLORS:
            raise ValueError(f"color must be one of: {', '.join(sorted(VALID_COLORS))}")
        return v


class BatchRatingUpdate(BaseModel):
    asset_ids: list[str]
    favorite: bool | None = None
    stars: int | None = None
    color: str | None = None

    _color_provided: bool = False

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("asset_ids must not be empty")
        if len(v) > 1000:
            raise ValueError("asset_ids must not exceed 1000")
        return v

    @field_validator("stars")
    @classmethod
    def validate_stars(cls, v: int | None) -> int | None:
        if v is not None and (v < 0 or v > 5):
            raise ValueError("stars must be between 0 and 5")
        return v

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_COLORS:
            raise ValueError(f"color must be one of: {', '.join(sorted(VALID_COLORS))}")
        return v


class RatingLookupRequest(BaseModel):
    asset_ids: list[str]

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, v: list[str]) -> list[str]:
        if len(v) > 1000:
            raise ValueError("asset_ids must not exceed 1000")
        return v


class RatingResponse(BaseModel):
    asset_id: str
    favorite: bool
    stars: int
    color: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_color(body: dict, raw_json: bytes) -> str | object:
    """Determine if color was explicitly provided in the request body."""
    import json
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return _SENTINEL
    if "color" in parsed:
        return parsed["color"]  # Could be None (explicit null) or a string
    return _SENTINEL


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.put("/{asset_id}/rating")
async def rate_asset(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> RatingResponse:
    """Set or update rating on a single asset."""
    raw_body = await request.body()
    import json
    try:
        body_dict = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_dict = {}

    try:
        body = RatingUpdate(**body_dict)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Verify asset exists and is active
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    color_value = _parse_color(body_dict, raw_body)
    rating_repo = RatingRepository(session)
    result = rating_repo.upsert(
        user_id,
        asset_id,
        favorite=body.favorite,
        stars=body.stars,
        color=color_value,
    )

    if result is None:
        return RatingResponse(asset_id=asset_id, favorite=False, stars=0, color=None)
    return RatingResponse(
        asset_id=asset_id,
        favorite=result.favorite,
        stars=result.stars,
        color=result.color,
    )


@router.put("/ratings")
async def batch_rate_assets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    """Apply the same rating update to multiple assets."""
    raw_body = await request.body()
    import json
    try:
        body_dict = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        body = BatchRatingUpdate(**body_dict)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Verify all assets exist and are active
    asset_repo = AssetRepository(session)
    for aid in body.asset_ids:
        asset = asset_repo.get_by_id(aid)
        if asset is None or asset.deleted_at is not None:
            raise HTTPException(status_code=404, detail=f"Asset not found: {aid}")

    color_value = _parse_color(body_dict, raw_body)
    rating_repo = RatingRepository(session)
    updated = rating_repo.batch_upsert(
        user_id,
        body.asset_ids,
        favorite=body.favorite,
        stars=body.stars,
        color=color_value,
    )
    return {"updated": updated}


@router.post("/ratings/lookup")
def lookup_ratings(
    body: RatingLookupRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    """Bulk read ratings for a list of assets. Returns map of asset_id → rating."""
    rating_repo = RatingRepository(session)
    ratings = rating_repo.get_for_assets(user_id, body.asset_ids)
    return {
        "ratings": {
            aid: {
                "favorite": r.favorite,
                "stars": r.stars,
                "color": r.color,
            }
            for aid, r in ratings.items()
        }
    }


@router.get("/favorites")
def list_favorites(
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    after: str | None = None,
    limit: int = 200,
) -> dict:
    """List favorited assets across all libraries, newest first."""
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1

    rating_repo = RatingRepository(session)
    assets, next_cursor = rating_repo.list_favorites(user_id, after=after, limit=limit)

    # Look up library names for grouping
    lib_repo = LibraryRepository(session)
    lib_ids = list({a.library_id for a in assets})
    libs_by_id = {}
    for lid in lib_ids:
        lib = lib_repo.get_by_id(lid)
        if lib:
            libs_by_id[lid] = lib.name

    items = [
        {
            "asset_id": a.asset_id,
            "library_id": a.library_id,
            "library_name": libs_by_id.get(a.library_id, ""),
            "rel_path": a.rel_path,
            "file_size": a.file_size,
            "file_mtime": a.file_mtime.isoformat() if a.file_mtime else None,
            "media_type": a.media_type,
            "width": a.width,
            "height": a.height,
            "taken_at": a.taken_at.isoformat() if a.taken_at else None,
            "status": a.status,
            "duration_sec": a.duration_sec,
            "camera_make": a.camera_make,
            "camera_model": a.camera_model,
        }
        for a in assets
    ]

    return {"items": items, "next_cursor": next_cursor}
