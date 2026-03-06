"""Assets API: upsert for scanner. All routes require tenant auth (middleware)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository, LibraryRepository, ScanRepository

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


@router.get("", response_model=list[AssetResponse])
def list_assets(
    library_id: str | None = None,
    session: Annotated[Session, Depends(get_tenant_session)] = None,
) -> list[AssetResponse]:
    """List assets, optionally filtered by library_id."""
    asset_repo = AssetRepository(session)
    assets = asset_repo.list_by_library(library_id) if library_id else asset_repo.list_all()
    return [
        AssetResponse(
            asset_id=a.asset_id,
            library_id=a.library_id,
            rel_path=a.rel_path,
            media_type=a.media_type,
            status=a.status,
            proxy_key=a.proxy_key,
            thumbnail_key=a.thumbnail_key,
            width=a.width,
            height=a.height,
        )
        for a in assets
    ]


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
    )


@router.post("/upsert", response_model=UpsertAssetResponse)
def upsert_asset(
    body: UpsertAssetRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> UpsertAssetResponse:
    """
    Upsert an asset by (library_id, rel_path). Creates if not found; otherwise
    updates or skips based on force flag and existing sha256/size/mtime.
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
