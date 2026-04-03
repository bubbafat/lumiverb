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

from src.api.dependencies import get_current_user_id, get_tenant_session
from src.core import asset_status
from src.core.io_utils import normalize_path_prefix
from src.repository.tenant import AssetMetadataRepository, AssetRepository, LibraryRepository
from src.storage.local import get_storage
from src.core.utils import utcnow

logger = logging.getLogger(__name__)



router = APIRouter(prefix="/v1/assets", tags=["assets"])


class UpsertAssetRequest(BaseModel):
    library_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    media_type: str
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
    ocr_text: str | None = None
    transcript_srt: str | None = None
    transcript_language: str | None = None
    transcribed_at: str | None = None
    note: str | None = None
    note_author: str | None = None
    note_updated_at: str | None = None


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
    face_count: int | None = None
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
    missing_embeddings: bool = False,
    missing_faces: bool = False,
    missing_video_scenes: bool = False,
    missing_ocr: bool = False,
    missing_scene_vision: bool = False,
    has_faces: bool | None = None,
    person_id: str | None = None,
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

    # Rating filters require user identity
    rating_user_id: str | None = None
    color_list: list[str] | None = None
    needs_rating_filter = favorite is not None or star_min is not None or star_max is not None or color is not None or has_rating is not None
    if needs_rating_filter:
        uid = getattr(request.state, "user_id", None)
        if not uid:
            key_id = getattr(request.state, "key_id", None)
            rating_user_id = f"key:{key_id}" if key_id else None
        else:
            rating_user_id = uid
        if color is not None:
            color_list = [c.strip() for c in color.split(",") if c.strip()]

    assets = asset_repo.page_by_library(
        library_id=library_id,
        after=after,
        limit=limit,
        path_prefix=normalized_prefix,
        tag=tag,
        missing_vision=missing_vision,
        missing_embeddings=missing_embeddings,
        missing_faces=missing_faces,
        missing_video_scenes=missing_video_scenes,
        missing_ocr=missing_ocr,
        missing_scene_vision=missing_scene_vision,
        has_faces=has_faces,
        person_id=person_id,
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

    return AssetPageResponse(items=items, next_cursor=next_cursor)


class RepairSummary(BaseModel):
    total_assets: int = 0
    missing_proxy: int = 0
    missing_exif: int = 0
    missing_vision: int = 0
    missing_embeddings: int = 0
    missing_faces: int = 0
    missing_ocr: int = 0
    missing_video_scenes: int = 0
    missing_scene_vision: int = 0
    stale_search_sync: int = 0


@router.get("/repair-summary", response_model=RepairSummary)
def repair_summary(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
) -> RepairSummary:
    """Count assets missing various pipeline outputs for a library."""
    from sqlalchemy import text
    lib = LibraryRepository(session).get_by_id(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="Library not found")
    from src.repository.tenant import MISSING_CONDITIONS
    row = session.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE proxy_key IS NULL) AS missing_proxy,
                COUNT(*) FILTER (WHERE exif_extracted_at IS NULL AND media_type = 'image') AS missing_exif,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_vision"]}) AS missing_vision,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_embeddings"]}) AS missing_embeddings,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_faces"]}) AS missing_faces,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_ocr"]}) AS missing_ocr,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_video_scenes"]}) AS missing_video_scenes,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_scene_vision"]}) AS missing_scene_vision,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM asset_metadata am
                        WHERE am.asset_id = a.asset_id
                    ) AND (
                        a.search_synced_at IS NULL
                        OR a.search_synced_at < (
                            SELECT MAX(am2.generated_at)
                            FROM asset_metadata am2
                            WHERE am2.asset_id = a.asset_id
                        )
                    )
                ) AS stale_search_sync
            FROM active_assets a
            WHERE library_id = :library_id
        """),
        {"library_id": library_id},
    ).one()
    return RepairSummary(
        total_assets=row.total,
        missing_proxy=row.missing_proxy,
        missing_exif=row.missing_exif,
        missing_vision=row.missing_vision,
        missing_embeddings=row.missing_embeddings,
        missing_faces=row.missing_faces,
        missing_ocr=row.missing_ocr,
        missing_video_scenes=row.missing_video_scenes,
        missing_scene_vision=row.missing_scene_vision,
        stale_search_sync=row.stale_search_sync,
    )


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
        public_collection_id = request.query_params.get("public_collection_id")
        if public_library_id:
            if asset.library_id != public_library_id:
                raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
            lib = LibraryRepository(session).get_by_id(public_library_id)
            if lib is None or not lib.is_public:
                raise HTTPException(status_code=404, detail="Not found")
        elif public_collection_id:
            from src.repository.tenant import CollectionRepository, CollectionAsset
            from src.models.tenant import Collection
            col_repo = CollectionRepository(session)
            col = col_repo.get_by_id(public_collection_id)
            if col is None or col.visibility != "public":
                raise HTTPException(status_code=404, detail="Not found")
            # Verify asset is in this collection
            from sqlmodel import select
            membership = session.exec(
                select(CollectionAsset).where(
                    CollectionAsset.collection_id == public_collection_id,
                    CollectionAsset.asset_id == asset_id,
                )
            ).first()
            if membership is None:
                raise HTTPException(status_code=403, detail="Asset not in collection")
        else:
            raise HTTPException(status_code=403, detail="Public access requires library or collection context")

    key = asset.proxy_key if size == "proxy" else asset.thumbnail_key
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"No {size} available for this asset",
        )

    storage = get_storage()
    path = storage.abs_path(key)
    if not path.exists():
        # Stale key: file was lost. Clear it so next ingest regenerates it.
        if size == "proxy":
            asset.proxy_key = None
        else:
            asset.thumbnail_key = None
        session.add(asset)
        session.commit()
        raise HTTPException(status_code=404, detail=f"No {size} available for this asset")

    key_ext = Path(key).suffix.lower()
    if key_ext == ".webp":
        content_type = "image/webp"
        filename = Path(asset.rel_path).stem + ".webp"
    else:
        content_type = "image/jpeg"
        filename = Path(asset.rel_path).stem + ".jpg"

    # HTTP headers must be latin-1 encodable. macOS screenshot filenames
    # contain \u202f (narrow no-break space) which is not latin-1 safe.
    # Use RFC 5987 filename* for the full UTF-8 name, and a sanitized
    # ASCII fallback for the plain filename.
    from urllib.parse import quote
    ascii_filename = filename.encode("ascii", errors="replace").decode("ascii")
    utf8_filename = quote(filename)

    def _iter() -> bytes:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{utf8_filename}"
            ),
        },
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
        transcript_srt=asset.transcript_srt,
        transcript_language=asset.transcript_language,
        transcribed_at=asset.transcribed_at.isoformat() if asset.transcribed_at else None,
        note=asset.note,
        note_author=asset.note_author,
        note_updated_at=asset.note_updated_at.isoformat() if asset.note_updated_at else None,
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
    ocr_text: str | None = None

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []
        ocr_text = meta.data.get("ocr_text") or None

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    response.ocr_text = ocr_text
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
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> BatchTrashResponse:
    """Soft-delete multiple assets. Returns trashed and not_found lists. Quickwit delete is best-effort."""
    asset_repo = AssetRepository(session)
    trashed_ids, not_found_ids = asset_repo.trash_many(body.asset_ids)
    if trashed_ids:
        try:
            from src.search.quickwit_client import QuickwitClient
            qw = QuickwitClient()
            tenant_id = getattr(request.state, "tenant_id", None)
            for aid in trashed_ids:
                if tenant_id:
                    qw.delete_tenant_documents_by_asset_id(tenant_id, aid)
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
    ocr_text: str | None = None

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []
        ocr_text = meta.data.get("ocr_text") or None

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    response.ocr_text = ocr_text
    return response


@router.delete("/{asset_id}", status_code=204)
def trash_asset(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Soft-delete a single asset. 404 if not found or already trashed. Quickwit delete is best-effort."""
    asset_repo = AssetRepository(session)
    ok = asset_repo.trash(asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Asset not found or already trashed")
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        try:
            from src.search.quickwit_client import QuickwitClient
            QuickwitClient().delete_tenant_documents_by_asset_id(tenant_id, asset_id)
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
    request: Request,
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
    AssetRepository(session).set_status(asset_id, asset_status.DESCRIBED)

    # Inline search sync (best-effort)
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    if meta:
        from src.search.sync import try_sync_asset
        try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    # Bump library revision for UI polling
    LibraryRepository(session).bump_revision(asset.library_id)

    return VisionSubmitResponse(asset_id=asset_id, status="described")


class OcrSubmitRequest(BaseModel):
    ocr_text: str


@router.post("/{asset_id}/ocr", status_code=200)
def submit_ocr(
    asset_id: str,
    body: OcrSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit OCR text for an asset. Merges into existing metadata."""
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset_id)
    if meta is None:
        raise HTTPException(status_code=400, detail="Asset has no vision metadata — run vision first")

    # Merge ocr_text into existing metadata data dict
    data = dict(meta.data) if meta.data else {}
    data["ocr_text"] = body.ocr_text
    meta_repo.upsert(
        asset_id=asset_id,
        model_id=meta.model_id,
        model_version=meta.model_version,
        data=data,
    )

    # Re-sync search
    meta = meta_repo.get_latest(asset_id=asset_id)
    from src.search.sync import try_sync_asset
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return {"asset_id": asset_id, "ocr_text": body.ocr_text}


class BatchOcrItem(BaseModel):
    asset_id: str
    ocr_text: str


class BatchOcrRequest(BaseModel):
    items: list[BatchOcrItem]


@router.post("/batch-ocr", status_code=200)
def submit_batch_ocr(
    body: BatchOcrRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit OCR text for multiple assets in one request."""
    meta_repo = AssetMetadataRepository(session)
    asset_repo = AssetRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        meta = meta_repo.get_latest(asset_id=item.asset_id)
        if meta is None:
            skipped += 1
            continue
        data = dict(meta.data) if meta.data else {}
        data["ocr_text"] = item.ocr_text
        meta_repo.upsert(
            asset_id=item.asset_id,
            model_id=meta.model_id,
            model_version=meta.model_version,
            data=data,
        )
        updated += 1

    # Clear search_synced_at so the sweep picks these up for re-indexing.
    # Avoids 25 individual Quickwit calls inside the request.
    if updated > 0:
        from sqlalchemy import text
        asset_ids = [item.asset_id for item in body.items]
        session.execute(
            text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = ANY(:ids)"),
            {"ids": asset_ids},
        )
        session.commit()

        # Bump revision once per library
        lib_ids = {a.library_id for item in body.items if (a := asset_repo.get_by_id(item.asset_id))}
        lib_repo = LibraryRepository(session)
        for lid in lib_ids:
            lib_repo.bump_revision(lid)

    return {"updated": updated, "skipped": skipped}


class TranscriptSubmitRequest(BaseModel):
    srt: str
    language: str | None = None
    source: str = "manual"


class TranscriptSubmitResponse(BaseModel):
    asset_id: str
    status: str


@router.post("/{asset_id}/transcript", response_model=TranscriptSubmitResponse)
def submit_transcript(
    asset_id: str,
    body: TranscriptSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TranscriptSubmitResponse:
    """Upload or replace an SRT transcript for a video asset."""
    from src.core.srt import parse_srt_to_text, validate_srt

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Transcripts are only supported for video assets")
    if not validate_srt(body.srt):
        raise HTTPException(status_code=400, detail="Invalid SRT format")

    plain_text = parse_srt_to_text(body.srt)

    asset.transcript_srt = body.srt
    asset.transcript_text = plain_text
    asset.transcript_language = body.language
    asset.transcribed_at = utcnow()
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search (works with or without vision metadata)
    from src.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return TranscriptSubmitResponse(asset_id=asset_id, status="transcribed")


@router.delete("/{asset_id}/transcript", status_code=204)
def delete_transcript(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Remove a transcript from a video asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.transcript_srt = None
    asset.transcript_text = None
    asset.transcript_language = None
    asset.transcribed_at = None
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search
    from src.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)


class NoteUpdateRequest(BaseModel):
    text: str


class NoteUpdateResponse(BaseModel):
    asset_id: str
    note: str | None
    note_author: str | None
    note_updated_at: str | None


@router.put("/{asset_id}/note", response_model=NoteUpdateResponse)
def update_note(
    asset_id: str,
    body: NoteUpdateRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> NoteUpdateResponse:
    """Add, update, or clear a freeform note on an asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    text = body.text.strip() if body.text else ""
    if text:
        # Resolve email for display
        email = getattr(request.state, "email", None) or user_id
        asset.note = text
        asset.note_author = email
        asset.note_updated_at = utcnow()
    else:
        asset.note = None
        asset.note_author = None
        asset.note_updated_at = None

    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search (works with or without vision metadata)
    from src.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return NoteUpdateResponse(
        asset_id=asset_id,
        note=asset.note,
        note_author=asset.note_author,
        note_updated_at=asset.note_updated_at.isoformat() if asset.note_updated_at else None,
    )


@router.delete("/{asset_id}/note", status_code=204)
def delete_note(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Delete a note from an asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.note = None
    asset.note_author = None
    asset.note_updated_at = None
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    from src.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)


class EmbeddingSubmitRequest(BaseModel):
    model_id: str
    model_version: str
    vector: list[float]


@router.post("/{asset_id}/embeddings", status_code=201)
def submit_embedding(
    asset_id: str,
    body: EmbeddingSubmitRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit an embedding vector for an asset."""
    from src.repository.tenant import AssetEmbeddingRepository
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    emb_repo = AssetEmbeddingRepository(session)
    emb_repo.upsert(
        asset_id=asset_id,
        model_id=body.model_id,
        model_version=body.model_version,
        vector=[float(x) for x in body.vector],
    )
    return {"ok": True}


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

        # File is missing on disk – clear key.
        asset.video_preview_key = None
        session.add(asset)
        session.commit()

    raise HTTPException(status_code=404, detail="No video preview available for this asset")


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
    Upsert by (library_id, rel_path): creates if not found; otherwise updates or skips.
    """
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(body.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")

    file_mtime_dt: datetime | None = None
    if body.file_mtime:
        try:
            file_mtime_dt = datetime.fromisoformat(body.file_mtime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid file_mtime format")

    asset_repo = AssetRepository(session)
    existing = asset_repo.get_by_library_and_rel_path(body.library_id, body.rel_path)

    if existing is None:
        asset_repo.create_asset(
            library_id=body.library_id,
            rel_path=body.rel_path,
            file_size=body.file_size,
            file_mtime=file_mtime_dt,
            media_type=body.media_type,
        )
        return UpsertAssetResponse(action="added")

    if body.force or existing.file_size != body.file_size or existing.file_mtime != file_mtime_dt:
        existing.file_size = body.file_size
        existing.file_mtime = file_mtime_dt
        existing.availability = "online"
        session.add(existing)
        session.commit()
        return UpsertAssetResponse(action="updated")

    return UpsertAssetResponse(action="skipped")


# ---------------------------------------------------------------------------
# Face detection endpoints (ADR-009)
# ---------------------------------------------------------------------------


class FaceDetectionItem(BaseModel):
    bounding_box: dict[str, float]
    detection_confidence: float
    embedding: list[float] | None = None


class FaceSubmitRequest(BaseModel):
    detection_model: str = "insightface"
    detection_model_version: str = "buffalo_l"
    faces: list[FaceDetectionItem]


class FaceSubmitResponse(BaseModel):
    face_count: int
    face_ids: list[str]


class FaceListItem(BaseModel):
    face_id: str
    bounding_box: dict | None
    detection_confidence: float | None
    person: dict | None = None


class FaceListResponse(BaseModel):
    faces: list[FaceListItem]


def _generate_face_crops(
    tenant_id: str,
    asset: object,
    face_ids: list[str],
    faces_data: list[dict],
    session: object,
) -> None:
    """Generate 128x128 WebP face crop thumbnails from the asset proxy."""
    import io
    from PIL import Image

    from src.storage.local import get_storage

    storage = get_storage()
    proxy_key = asset.proxy_key  # type: ignore[union-attr]
    if not proxy_key:
        return

    proxy_path = storage.abs_path(proxy_key)
    if not proxy_path.exists():
        return

    try:
        img = Image.open(proxy_path).convert("RGB")
    except Exception:
        logger.warning("Cannot open proxy for face crops: %s", proxy_key)
        return

    try:
        w, h = img.size

        for face_id, face_data in zip(face_ids, faces_data):
            bb = face_data.get("bounding_box")
            if not bb:
                continue

            # Expand bounding box by 40% padding, clamp to image bounds
            pad = 0.4
            fx, fy, fw, fh = bb["x"], bb["y"], bb["w"], bb["h"]
            cx, cy = fx + fw / 2, fy + fh / 2
            side = max(fw, fh) * (1 + pad)
            x1 = max(0.0, cx - side / 2)
            y1 = max(0.0, cy - side / 2)
            x2 = min(1.0, cx + side / 2)
            y2 = min(1.0, cy + side / 2)

            # Convert fractions to pixels
            px1, py1 = int(x1 * w), int(y1 * h)
            px2, py2 = int(x2 * w), int(y2 * h)
            if px2 <= px1 or py2 <= py1:
                continue

            try:
                crop = img.crop((px1, py1, px2, py2))
                crop = crop.resize((128, 128), Image.LANCZOS)
                buf = io.BytesIO()
                crop.save(buf, format="WEBP", quality=80)
                crop_bytes = buf.getvalue()

                crop_key = storage.face_crop_key(tenant_id, asset.library_id, face_id)  # type: ignore[union-attr]
                storage.write(crop_key, crop_bytes)

                # Update face record
                from sqlalchemy import text as sa_text
                session.execute(  # type: ignore[union-attr]
                    sa_text("UPDATE faces SET crop_key = :key WHERE face_id = :fid"),
                    {"key": crop_key, "fid": face_id},
                )
            except Exception:
                logger.warning("Failed to generate crop for face %s", face_id, exc_info=True)

        session.commit()  # type: ignore[union-attr]
    finally:
        img.close()


class BatchFaceItem(BaseModel):
    asset_id: str
    detection_model: str = "insightface"
    detection_model_version: str = "buffalo_l"
    faces: list[FaceDetectionItem]


class BatchFaceRequest(BaseModel):
    items: list[BatchFaceItem]


@router.post("/batch-faces", status_code=200)
def submit_batch_faces(
    body: BatchFaceRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit face detections for multiple assets in one request."""
    from src.repository.tenant import FaceRepository

    asset_repo = AssetRepository(session)
    face_repo = FaceRepository(session)
    tenant_id = getattr(request.state, "tenant_id", None)
    processed = 0
    skipped = 0

    lib_ids: set[str] = set()
    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue

        faces_data = [
            {
                "bounding_box": f.bounding_box,
                "detection_confidence": f.detection_confidence,
                "embedding": [float(x) for x in f.embedding] if f.embedding else None,
            }
            for f in item.faces
        ]
        face_ids = face_repo.submit_faces(
            asset_id=item.asset_id,
            detection_model=item.detection_model,
            detection_model_version=item.detection_model_version,
            faces=faces_data,
        )

        if tenant_id and asset.proxy_key:
            _generate_face_crops(tenant_id, asset, face_ids, faces_data, session)

        lib_ids.add(asset.library_id)
        processed += 1

    # Bump revision once per library
    lib_repo = LibraryRepository(session)
    for lid in lib_ids:
        lib_repo.bump_revision(lid)

    return {"processed": processed, "skipped": skipped}


@router.post("/{asset_id}/faces", response_model=FaceSubmitResponse, status_code=201)
def submit_faces(
    asset_id: str,
    body: FaceSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FaceSubmitResponse:
    """Submit face detections for an asset. Replaces existing faces for the same model."""
    from src.repository.tenant import FaceRepository

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    face_repo = FaceRepository(session)
    faces_data = [
        {
            "bounding_box": f.bounding_box,
            "detection_confidence": f.detection_confidence,
            "embedding": [float(x) for x in f.embedding] if f.embedding else None,
        }
        for f in body.faces
    ]
    face_ids = face_repo.submit_faces(
        asset_id=asset_id,
        detection_model=body.detection_model,
        detection_model_version=body.detection_model_version,
        faces=faces_data,
    )

    # Generate face crop thumbnails
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset.proxy_key:
        _generate_face_crops(tenant_id, asset, face_ids, faces_data, session)

    # Bump library revision so UI reflects face_count changes
    LibraryRepository(session).bump_revision(asset.library_id)

    return FaceSubmitResponse(face_count=len(face_ids), face_ids=face_ids)


@router.get("/{asset_id}/faces", response_model=FaceListResponse)
def list_faces(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FaceListResponse:
    """List all detected faces for an asset."""
    from src.repository.tenant import FaceRepository

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    face_repo = FaceRepository(session)
    faces = face_repo.get_by_asset_id(asset_id)
    face_ids = [f.face_id for f in faces]
    persons_by_face = face_repo.get_persons_for_faces(face_ids)

    return FaceListResponse(
        faces=[
            FaceListItem(
                face_id=f.face_id,
                bounding_box=f.bounding_box_json,
                detection_confidence=f.detection_confidence,
                person=(
                    {"person_id": p.person_id, "display_name": p.display_name, "dismissed": p.dismissed}
                    if (p := persons_by_face.get(f.face_id)) else None
                ),
            )
            for f in faces
        ]
    )
