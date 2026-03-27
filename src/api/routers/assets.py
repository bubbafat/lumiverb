"""Assets API: upsert for scanner, trash and restore. All routes require tenant auth (middleware)."""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core import asset_status
from src.core.io_utils import normalize_path_prefix
from src.repository.tenant import AssetMetadataRepository, AssetRepository, LibraryRepository, ScanRepository, SearchSyncQueueRepository, WorkerJobRepository
from src.storage.local import get_storage
from src.core.utils import utcnow

logger = logging.getLogger(__name__)


PRIORITY_URGENT = 0

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
    iso: int | None = None
    exposure_time_us: int | None = None
    aperture: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    lens_model: str | None = None
    flash_fired: bool | None = None
    orientation: int | None = None
    video_preview_key: str | None = None
    video_preview_generated_at: str | None = None  # ISO8601
    video_preview_last_accessed_at: str | None = None  # ISO8601
    duration_sec: float | None = None
    ai_description: str | None = None
    ai_tags: list[str] = []


class AssetPageItem(BaseModel):
    """Asset fields returned by GET /v1/assets/page."""

    asset_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    sha256: str | None
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None  # ISO8601
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
    created_at: str | None = None  # ISO8601


class AssetPageResponse(BaseModel):
    """Response envelope for paginated assets."""

    items: list[AssetPageItem]
    next_cursor: str | None = None


# Valid sort columns for the page endpoint.
SORT_COLUMNS = {"taken_at", "created_at", "file_size", "iso", "exposure_time_us", "aperture", "focal_length", "rel_path", "asset_id"}


def _encode_cursor(sort_col: str, sort_value: object, asset_id: str) -> str:
    """Encode a composite cursor as base64 JSON."""
    payload = json.dumps({"v": sort_value, "id": asset_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class BatchTrashRequest(BaseModel):
    asset_ids: list[str]


class BatchTrashResponse(BaseModel):
    trashed: list[str]
    not_found: list[str]


class StateCheckRequest(BaseModel):
    asset_ids: list[str]

    @field_validator("asset_ids")
    @classmethod
    def max_ids(cls, v: list[str]) -> list[str]:
        if len(v) == 0:
            raise ValueError("asset_ids must not be empty")
        if len(v) > 500:
            raise ValueError("Maximum 500 asset_ids per request")
        return v


class AssetStateItem(BaseModel):
    asset_id: str
    deleted: bool
    proxy_sha256: str | None


class StateCheckResponse(BaseModel):
    assets: list[AssetStateItem]


class VisionSubmitRequest(BaseModel):
    model_id: str
    model_version: str = "1"
    description: str
    tags: list[str] = []
    client_proxy_sha256: str | None = None


class VisionSubmitResponse(BaseModel):
    asset_id: str
    status: str


@router.get("/page", response_model=AssetPageResponse)
def page_assets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    after: str | None = None,
    limit: int = 500,
    path_prefix: str | None = None,
    tag: str | None = None,
    missing_vision: bool = False,
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
) -> AssetPageResponse:
    """
    Keyset-paginated assets with sorting and filtering.
    Returns a response envelope with items and next_cursor.
    """
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1
    if getattr(request.state, "is_public_request", False):
        lib_repo = LibraryRepository(session)
        library = lib_repo.get_by_id(library_id)
        if library is None or not library.is_public:
            raise HTTPException(status_code=404, detail="Not found")

    sort_col = sort if sort in SORT_COLUMNS else "taken_at"
    direction = dir if dir in ("asc", "desc") else "desc"

    asset_repo = AssetRepository(session)
    normalized_prefix: str | None = None
    if path_prefix is not None:
        normalized_prefix = normalize_path_prefix(path_prefix)
        if normalized_prefix and ".." in normalized_prefix.split("/"):
            raise HTTPException(
                status_code=400,
                detail="Invalid path_prefix; path traversal not allowed",
            )

    media_types: list[str] | None = None
    if media_type:
        media_types = [m.strip() for m in media_type.split(",") if m.strip()]

    assets = asset_repo.page_by_library(
        library_id=library_id,
        after=after,
        limit=limit,
        path_prefix=normalized_prefix,
        tag=tag,
        missing_vision=missing_vision,
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
    )

    items = [
        AssetPageItem(
            asset_id=a.asset_id,
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

    return AssetPageResponse(items=items, next_cursor=next_cursor)


@router.post("/state-check", response_model=StateCheckResponse)
def state_check(
    body: StateCheckRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StateCheckResponse:
    """Return deletion status and proxy_sha256 for a batch of asset IDs.

    Includes soft-deleted assets (client needs to evict them from cache).
    Asset IDs not found in the DB are returned with deleted=True, proxy_sha256=None.
    Maximum 500 IDs per request.
    """
    states = AssetRepository(session).get_states(body.asset_ids)
    items = [
        AssetStateItem(
            asset_id=aid,
            deleted=states[aid]["deleted"] if aid in states else True,
            proxy_sha256=states[aid]["proxy_sha256"] if aid in states else None,
        )
        for aid in body.asset_ids
    ]
    return StateCheckResponse(assets=items)


def _stream_asset_file(
    asset_id: str,
    size: str,  # "proxy" or "thumbnail"
    request: Request,
    session: Session,
) -> StreamingResponse:
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if getattr(request.state, "is_public_request", False):
        public_library_id = request.query_params.get("public_library_id")
        if not public_library_id or asset.library_id != public_library_id:
            raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
        lib = LibraryRepository(session).get_by_id(public_library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")

    key = asset.proxy_key if size == "proxy" else asset.thumbnail_key
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"No {size} available for this asset",
        )

    storage = get_storage()
    path = storage.abs_path(key)
    if not path.exists():
        # Stale key: file was lost. Clear it and re-enqueue so the worker regenerates it.
        job_repo = WorkerJobRepository(session)
        if size == "proxy":
            asset.proxy_key = None
            if not job_repo.has_pending_job("proxy", asset_id):
                job_repo.create(job_type="proxy", asset_id=asset_id, priority=PRIORITY_URGENT)
        else:
            # size == "thumbnail"
            asset.thumbnail_key = None
            if asset.media_type.startswith("video"):
                if not job_repo.has_pending_job("video-index", asset_id):
                    job_repo.create(job_type="video-index", asset_id=asset_id, priority=PRIORITY_URGENT)
            else:
                if not job_repo.has_pending_job("proxy", asset_id):
                    job_repo.create(job_type="proxy", asset_id=asset_id, priority=PRIORITY_URGENT)
        session.add(asset)
        session.commit()
        return JSONResponse({"status": "generating"}, status_code=202)

    key_ext = Path(key).suffix.lower()
    if key_ext == ".webp":
        content_type = "image/webp"
        filename = Path(asset.rel_path).stem + ".webp"
    else:
        content_type = "image/jpeg"
        filename = Path(asset.rel_path).stem + ".jpg"

    def _iter() -> bytes:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _stream_file_with_range(
    path: Path,
    request: Request,
    media_type: str,
) -> StreamingResponse:
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1
    status_code = 200
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}

    if range_header:
        # Format: bytes=start-end
        try:
            units, _, range_spec = range_header.partition("=")
            if units.strip().lower() == "bytes":
                start_str, _, end_str = range_spec.partition("-")
                if start_str:
                    start = int(start_str)
                if end_str:
                    end = int(end_str)
                if end >= file_size:
                    end = file_size - 1
                if start > end:
                    start = 0
                    end = file_size - 1
                status_code = 206
        except Exception:
            start = 0
            end = file_size - 1

    content_length = end - start + 1
    headers["Content-Length"] = str(content_length)
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    def file_iterator() -> bytes:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = content_length
            chunk_size = 65536
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                chunk = f.read(read_size)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        file_iterator(),
        media_type=media_type,
        status_code=status_code,
        headers=headers,
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
        iso=asset.iso,
        exposure_time_us=asset.exposure_time_us,
        aperture=asset.aperture,
        focal_length=asset.focal_length,
        focal_length_35mm=asset.focal_length_35mm,
        lens_model=asset.lens_model,
        flash_fired=asset.flash_fired,
        orientation=asset.orientation,
        video_preview_key=asset.video_preview_key,
        video_preview_generated_at=asset.video_preview_generated_at.isoformat()
        if asset.video_preview_generated_at
        else None,
        video_preview_last_accessed_at=asset.video_preview_last_accessed_at.isoformat()
        if asset.video_preview_last_accessed_at
        else None,
        duration_sec=asset.duration_sec,
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
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AssetResponse:
    """Return a single asset by library_id + rel_path. 404 if not found or trashed."""
    if getattr(request.state, "is_public_request", False):
        lib = LibraryRepository(session).get_by_id(library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {rel_path}")
    response = _to_asset_response(asset)
    ai_description: str | None = None
    ai_tags: list[str] = []

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    return response


@router.get("", response_model=list[AssetResponse])
def list_assets(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> list[AssetResponse]:
    """List active (non-trashed) assets, optionally filtered by library_id."""
    asset_repo = AssetRepository(session)
    assets = asset_repo.list_by_library(library_id) if library_id else asset_repo.list_all()
    return [_to_asset_response(a) for a in assets]


@router.delete("", response_model=BatchTrashResponse)
def batch_trash_assets(
    body: BatchTrashRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> BatchTrashResponse:
    """Soft-delete multiple assets. Returns trashed and not_found lists. Quickwit delete is best-effort."""
    asset_repo = AssetRepository(session)
    # Resolve library_id for Quickwit before trashing (get_by_ids only returns active assets).
    active = asset_repo.get_by_ids(body.asset_ids)
    library_by_id = {a.asset_id: a.library_id for a in active}
    trashed_ids, not_found_ids = asset_repo.trash_many(body.asset_ids)
    if trashed_ids:
        try:
            from src.search.quickwit_client import QuickwitClient
            qw = QuickwitClient()
            for aid in trashed_ids:
                lib_id = library_by_id.get(aid)
                if lib_id:
                    qw.delete_documents_by_asset_id(lib_id, aid)
        except Exception as e:
            logger.warning("Quickwit delete after batch trash failed: %s", e)
    return BatchTrashResponse(trashed=trashed_ids, not_found=not_found_ids)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    public_library_id: str | None = None,
) -> AssetResponse:
    """Return a single asset by id. 404 if not found or trashed."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if getattr(request.state, "is_public_request", False):
        if not public_library_id or asset.library_id != public_library_id:
            raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
        lib = LibraryRepository(session).get_by_id(public_library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")
    response = _to_asset_response(asset)
    ai_description: str | None = None
    ai_tags: list[str] = []

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    return response


@router.delete("/{asset_id}", status_code=204)
def trash_asset(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Soft-delete a single asset. 404 if not found or already trashed. Quickwit delete is best-effort."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    library_id = asset.library_id if asset is not None else None
    ok = asset_repo.trash(asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Asset not found or already trashed")
    if library_id:
        try:
            from src.search.quickwit_client import QuickwitClient
            QuickwitClient().delete_documents_by_asset_id(library_id, asset_id)
        except Exception as e:
            logger.warning("Quickwit delete after trash failed for %s: %s", asset_id, e)


@router.post("/{asset_id}/restore", status_code=204)
def restore_asset(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Restore a single trashed asset. 404 if not found or not trashed."""
    asset_repo = AssetRepository(session)
    ok = asset_repo.restore(asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Asset not found or not trashed")


@router.post("/{asset_id}/vision", response_model=VisionSubmitResponse)
def submit_vision(
    asset_id: str,
    body: VisionSubmitRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> VisionSubmitResponse:
    """Submit AI vision results for an asset with optional proxy hash validation.

    If client_proxy_sha256 is provided and the server has a stored hash, they must
    match — otherwise 409. If the server hash is null (pre-Phase 1 or no proxy yet),
    the check is skipped for backwards compatibility.
    """
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    if body.client_proxy_sha256 is not None and asset.proxy_sha256 is not None:
        if body.client_proxy_sha256 != asset.proxy_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "proxy_hash_mismatch",
                        "message": "Client proxy does not match server proxy. Re-download the proxy and retry.",
                    }
                },
            )

    AssetMetadataRepository(session).upsert(
        asset_id=asset_id,
        model_id=body.model_id,
        model_version=body.model_version,
        data={"description": body.description, "tags": body.tags},
    )
    SearchSyncQueueRepository(session).enqueue(asset_id=asset_id, operation="upsert")
    AssetRepository(session).set_status(asset_id, asset_status.DESCRIBED)

    # Bump library revision for UI polling
    LibraryRepository(session).bump_revision(asset.library_id)

    return VisionSubmitResponse(asset_id=asset_id, status="described")


def _enqueue_video_preview_job_if_needed(
    session: Session,
    asset_id: str,
) -> None:
    job_repo = WorkerJobRepository(session)
    if job_repo.has_pending_job("video-preview", asset_id):
        return
    job_repo.create(job_type="video-preview", asset_id=asset_id, priority=PRIORITY_URGENT)


@router.get("/{asset_id}/preview")
def stream_or_enqueue_preview(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
):
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if getattr(request.state, "is_public_request", False):
        public_library_id = request.query_params.get("public_library_id")
        if not public_library_id or asset.library_id != public_library_id:
            raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
        lib = LibraryRepository(session).get_by_id(public_library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")

    if not asset.media_type.startswith("video"):
        raise HTTPException(status_code=422, detail="Preview only supported for video assets")

    storage = get_storage()

    if asset.video_preview_key:
        path = storage.abs_path(asset.video_preview_key)
        if path.exists():
            now = utcnow()
            last = asset.video_preview_last_accessed_at
            if last is None or (now - last).total_seconds() > 300:
                asset.video_preview_last_accessed_at = now
                session.add(asset)
                session.commit()
            return _stream_file_with_range(path, request, media_type="video/mp4")

        # File is missing on disk – clear key and re-enqueue.
        asset.video_preview_key = None
        session.add(asset)
        session.commit()
        _enqueue_video_preview_job_if_needed(session, asset.asset_id)
        return JSONResponse({"status": "generating"}, status_code=202)

    # No preview yet – enqueue and return 202.
    _enqueue_video_preview_job_if_needed(session, asset.asset_id)
    return JSONResponse({"status": "generating"}, status_code=202)


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
