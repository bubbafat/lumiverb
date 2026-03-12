"""Assets API: upsert for scanner. All routes require tenant auth (middleware)."""

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository, LibraryRepository, ScanRepository
from src.storage.local import get_storage

router = APIRouter(prefix="/v1/assets", tags=["assets"])


class UpsertAssetRequest(BaseModel):
    library_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    media_type: str
    scan_id: str
    force: bool = False


class UpsertAssetResponse(BaseModel):
    action: str  # added | updated | skipped


class AssetResponse(BaseModel):
    asset_id: str
    library_id: str
    rel_path: str
    media_type: str
    status: str
    proxy_key: str | None
    thumbnail_key: str | None
    width: int | None
    height: int | None
    sha256: str | None = None
    exif_extracted_at: str | None = None  # ISO8601
    camera_make: str | None = None
    camera_model: str | None = None
    taken_at: str | None = None  # ISO8601
    gps_lat: float | None = None
    gps_lon: float | None = None


class AssetPageItem(BaseModel):
    """Asset fields returned by GET /v1/assets/page for bulk reconciliation."""

    asset_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    sha256: str | None
    media_type: str


@router.get("/page", responses={204: {"description": "No assets (end of pages)"}})
def page_assets(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    after: str | None = None,
    limit: int = 500,
) -> list[AssetPageItem]:
    """
    Keyset-paginated assets for bulk reconciliation. Returns 204 if no results.
    Query: library_id (required), after (cursor), limit (default 500, max 500).
    """
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1
    asset_repo = AssetRepository(session)
    assets = asset_repo.page_by_library(library_id=library_id, after=after, limit=limit)
    if not assets:
        from fastapi.responses import Response

        return Response(status_code=204)
    return [
        AssetPageItem(
            asset_id=a.asset_id,
            rel_path=a.rel_path,
            file_size=a.file_size,
            file_mtime=a.file_mtime.isoformat() if a.file_mtime else None,
            sha256=a.sha256,
            media_type=a.media_type,
        )
        for a in assets
    ]


def _stream_asset_file(
    asset_id: str,
    size: str,  # "proxy" or "thumbnail"
    request: Request,
    session: Session,
) -> StreamingResponse:
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    key = asset.proxy_key if size == "proxy" else asset.thumbnail_key
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"No {size} available for this asset",
        )

    storage = get_storage()
    path = storage.abs_path(key)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{size.capitalize()} file not found on disk",
        )

    filename = Path(asset.rel_path).stem + ".jpg"

    def _iter() -> bytes:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type="image/jpeg",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _to_asset_response(asset) -> AssetResponse:
    """Map an Asset ORM/model object to AssetResponse."""
    return AssetResponse(
        asset_id=asset.asset_id,
        library_id=asset.library_id,
        rel_path=asset.rel_path,
        media_type=asset.media_type,
        status=asset.status,
        proxy_key=asset.proxy_key,
        thumbnail_key=asset.thumbnail_key,
        width=asset.width,
        height=asset.height,
        sha256=asset.sha256,
        exif_extracted_at=asset.exif_extracted_at.isoformat() if asset.exif_extracted_at else None,
        camera_make=asset.camera_make,
        camera_model=asset.camera_model,
        taken_at=asset.taken_at.isoformat() if asset.taken_at else None,
        gps_lat=asset.gps_lat,
        gps_lon=asset.gps_lon,
    )


@router.get("/{asset_id}/proxy")
def stream_proxy(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StreamingResponse:
    """Stream the proxy JPEG for an asset."""
    return _stream_asset_file(asset_id, "proxy", request, session)


@router.get("/{asset_id}/thumbnail")
def stream_thumbnail(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StreamingResponse:
    """Stream the thumbnail JPEG for an asset."""
    return _stream_asset_file(asset_id, "thumbnail", request, session)


@router.get("/by-path", response_model=AssetResponse)
def get_asset_by_path(
    library_id: str,
    rel_path: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AssetResponse:
    """Return a single asset by library_id + rel_path. 404 if not found."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {rel_path}")
    return _to_asset_response(asset)


@router.get("", response_model=list[AssetResponse])
def list_assets(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> list[AssetResponse]:
    """List assets, optionally filtered by library_id."""
    asset_repo = AssetRepository(session)
    assets = asset_repo.list_by_library(library_id) if library_id else asset_repo.list_all()
    return [_to_asset_response(a) for a in assets]


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AssetResponse:
    """Return a single asset by id. 404 if not found."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _to_asset_response(asset)


class ThumbnailKeyUpdateRequest(BaseModel):
    thumbnail_key: str


@router.post("/{asset_id}/thumbnail-key")
def set_thumbnail_key(
    asset_id: str,
    body: ThumbnailKeyUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Record a thumbnail_key for a video asset after the index worker extracts the first frame."""
    asset_repo = AssetRepository(session)
    asset_repo.update_thumbnail_key(asset_id, body.thumbnail_key)
    return {"asset_id": asset_id, "thumbnail_key": body.thumbnail_key}


@router.post("/upsert", response_model=UpsertAssetResponse)
def upsert_asset(
    body: UpsertAssetRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> UpsertAssetResponse:
    """
    Legacy single-file upsert. Prefer POST /v1/scans/{scan_id}/batch for bulk operations.
    Upsert by (library_id, rel_path): creates if not found; otherwise updates or skips.
    """
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(body.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    scan_repo = ScanRepository(session)
    scan = scan_repo.get_by_id(body.scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")

    file_mtime_dt: datetime | None = None
    if body.file_mtime:
        try:
            file_mtime_dt = datetime.fromisoformat(body.file_mtime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid file_mtime format")

    asset_repo = AssetRepository(session)
    existing = asset_repo.get_by_library_and_rel_path(body.library_id, body.rel_path)

    if existing is None:
        asset_repo.create_for_scan(
            library_id=body.library_id,
            rel_path=body.rel_path,
            file_size=body.file_size,
            file_mtime=file_mtime_dt,
            media_type=body.media_type,
            scan_id=body.scan_id,
        )
        return UpsertAssetResponse(action="added")

    if body.force:
        asset_repo.update_for_scan(
            asset_id=existing.asset_id,
            file_size=body.file_size,
            file_mtime=file_mtime_dt,
            availability="online",
            status="pending",
            last_scan_id=body.scan_id,
        )
        return UpsertAssetResponse(action="updated")

    if (
        existing.sha256 is not None
        and existing.file_size == body.file_size
        and existing.file_mtime == file_mtime_dt
    ):
        asset_repo.touch_for_scan(existing.asset_id, body.scan_id)
        return UpsertAssetResponse(action="skipped")

    asset_repo.update_for_scan(
        asset_id=existing.asset_id,
        file_size=body.file_size,
        file_mtime=file_mtime_dt,
        availability="online",
        status="pending",
        last_scan_id=body.scan_id,
    )
    return UpsertAssetResponse(action="updated")
