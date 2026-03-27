"""Atomic ingest endpoints: create + populate assets in one request.

POST /v1/ingest — create asset record AND ingest proxy + metadata atomically.
POST /v1/assets/{asset_id}/ingest — ingest into an existing asset record.

The server normalizes the proxy (WebP, 2048px max), generates a thumbnail
(WebP, 512px), and stores all provided metadata atomically.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from PIL import Image
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core import asset_status
from src.core.path_filter import PathFilter, is_path_included_merged
from src.repository.tenant import (
    AssetEmbeddingRepository,
    AssetMetadataRepository,
    AssetRepository,
    LibraryRepository,
    PathFilterRepository,
    SearchSyncQueueRepository,
)
from src.storage.local import LocalStorage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

PROXY_MAX_LONG_EDGE = 2048
THUMBNAIL_LONG_EDGE = 512
WEBP_QUALITY = 80
MAX_PROXY_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB raw upload limit


def _normalize_proxy(data: bytes) -> tuple[bytes, int, int]:
    """Decode image, resize to PROXY_MAX_LONG_EDGE if larger, encode as WebP.

    If the input is already WebP and within size limits, it is returned as-is
    (no re-encoding). Returns (webp_bytes, width, height) where width/height
    are the dimensions of the normalized proxy (not the original source).
    """
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    long_edge = max(w, h)

    # Fast path: already WebP and within size limits — skip re-encoding
    if img.format == "WEBP" and long_edge <= PROXY_MAX_LONG_EDGE:
        return data, w, h

    img = img.convert("RGB")
    if long_edge > PROXY_MAX_LONG_EDGE:
        scale = PROXY_MAX_LONG_EDGE / long_edge
        w = int(w * scale)
        h = int(h * scale)
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue(), w, h


def _generate_thumbnail(proxy_bytes: bytes) -> bytes:
    """Generate a thumbnail from proxy bytes. Returns WebP bytes."""
    img = Image.open(io.BytesIO(proxy_bytes))
    img = img.convert("RGB")

    w, h = img.size
    long_edge = max(w, h)
    if long_edge > THUMBNAIL_LONG_EDGE:
        scale = THUMBNAIL_LONG_EDGE / long_edge
        w = int(w * scale)
        h = int(h * scale)
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=WEBP_QUALITY)
    return buf.getvalue()


class IngestResponse(BaseModel):
    asset_id: str
    proxy_key: str
    proxy_sha256: str
    thumbnail_key: str
    thumbnail_sha256: str
    status: str
    width: int
    height: int
    created: bool = False


def _parse_optional_json(field: str | None, field_name: str) -> dict | None:
    if field is None:
        return None
    try:
        return json.loads(field)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON")


def _parse_optional_json_list(field: str | None, field_name: str) -> list[dict] | None:
    if field is None:
        return None
    try:
        data = json.loads(field)
        if not isinstance(data, list):
            raise ValueError
        return data
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON array")


def _do_ingest(
    *,
    asset_id: str,
    library_id: str,
    rel_path: str,
    tenant_id: str,
    raw_proxy: bytes,
    width: int | None,
    height: int | None,
    exif_data: dict | None,
    vision_data: dict | None,
    embeddings_data: list[dict] | None,
    session: Session,
) -> IngestResponse:
    """Core ingest logic shared by both endpoints."""
    storage: LocalStorage = get_storage()
    asset_repo = AssetRepository(session)

    # --- Normalize proxy to WebP ---
    try:
        proxy_bytes, proxy_w, proxy_h = _normalize_proxy(raw_proxy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process proxy image: {e}")

    # --- Generate thumbnail from normalized proxy ---
    thumb_bytes = _generate_thumbnail(proxy_bytes)

    # --- Write proxy to storage ---
    proxy_key = storage.proxy_key(tenant_id, library_id, asset_id, rel_path)
    proxy_path = storage.abs_path(proxy_key)
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_path.write_bytes(proxy_bytes)
    proxy_sha256 = hashlib.sha256(proxy_bytes).hexdigest()

    # --- Write thumbnail to storage ---
    thumb_key = storage.thumbnail_key(tenant_id, library_id, asset_id, rel_path)
    thumb_path = storage.abs_path(thumb_key)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(thumb_bytes)
    thumb_sha256 = hashlib.sha256(thumb_bytes).hexdigest()

    # --- Update DB: proxy + thumbnail ---
    source_w = width if width is not None else proxy_w
    source_h = height if height is not None else proxy_h
    asset_repo.set_proxy_artifact(asset_id, proxy_key, proxy_sha256, source_w, source_h)
    asset_repo.set_thumbnail_artifact(asset_id, thumb_key, thumb_sha256)

    final_status = asset_status.PROXY_READY

    # --- Store EXIF if provided ---
    if exif_data is not None:
        asset_repo.update_exif(
            asset_id=asset_id,
            sha256=exif_data.get("sha256"),
            exif=exif_data.get("exif", {}),
            camera_make=exif_data.get("camera_make"),
            camera_model=exif_data.get("camera_model"),
            taken_at=exif_data.get("taken_at"),
            gps_lat=exif_data.get("gps_lat"),
            gps_lon=exif_data.get("gps_lon"),
            duration_sec=exif_data.get("duration_sec"),
            iso=exif_data.get("iso"),
            shutter_speed=exif_data.get("shutter_speed"),
            aperture=exif_data.get("aperture"),
            focal_length=exif_data.get("focal_length"),
            focal_length_35mm=exif_data.get("focal_length_35mm"),
            lens_model=exif_data.get("lens_model"),
            flash_fired=exif_data.get("flash_fired"),
            orientation=exif_data.get("orientation"),
        )

    # --- Store vision results if provided ---
    if vision_data is not None:
        model_id = vision_data.get("model_id", "")
        model_version = vision_data.get("model_version", "1")
        description = vision_data.get("description", "")
        tags = vision_data.get("tags", [])

        if model_id:
            meta_repo = AssetMetadataRepository(session)
            meta_repo.upsert(
                asset_id=asset_id,
                model_id=model_id,
                model_version=model_version,
                data={"description": description, "tags": tags},
            )
            queue_repo = SearchSyncQueueRepository(session)
            queue_repo.enqueue(asset_id=asset_id, operation="upsert")
            final_status = asset_status.DESCRIBED

    # --- Store embeddings if provided ---
    if embeddings_data is not None:
        emb_repo = AssetEmbeddingRepository(session)
        for item in embeddings_data:
            model_id = item.get("model_id")
            model_version = item.get("model_version")
            vector = item.get("vector")
            if not model_id or not model_version or not isinstance(vector, list):
                raise HTTPException(
                    status_code=400,
                    detail="Each embedding must have model_id, model_version, and vector",
                )
            emb_repo.upsert(
                asset_id=asset_id,
                model_id=model_id,
                model_version=model_version,
                vector=[float(x) for x in vector],
            )

    # --- Set final status ---
    asset_repo.set_status(asset_id, final_status)

    # --- Bump library revision for UI polling ---
    LibraryRepository(session).bump_revision(library_id)

    return IngestResponse(
        asset_id=asset_id,
        proxy_key=proxy_key,
        proxy_sha256=proxy_sha256,
        thumbnail_key=thumb_key,
        thumbnail_sha256=thumb_sha256,
        status=final_status,
        width=source_w,
        height=source_h,
    )


# ---------------------------------------------------------------------------
# POST /v1/ingest — create asset + ingest atomically
# ---------------------------------------------------------------------------


@router.post("/v1/ingest", response_model=IngestResponse)
async def create_and_ingest(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    proxy: UploadFile = File(...),
    library_id: str = Form(...),
    rel_path: str = Form(...),
    file_size: int = Form(...),
    file_mtime: str | None = Form(default=None),
    media_type: str = Form(default="image/jpeg"),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    exif: str | None = Form(default=None),
    vision: str | None = Form(default=None),
    embeddings: str | None = Form(default=None),
) -> IngestResponse:
    """Create an asset record and ingest proxy + metadata in one atomic request.

    The asset only appears on the server once it's fully populated — no
    partial state. If an asset with the same (library_id, rel_path) already
    exists, it is updated (idempotent).

    Required form fields:
      - proxy: image file
      - library_id: target library
      - rel_path: relative path within the library root
      - file_size: source file size in bytes

    Optional:
      - file_mtime: ISO8601 timestamp of source file
      - media_type: MIME type (default image/jpeg)
      - width/height: original source dimensions
      - exif, vision, embeddings: JSON strings (same as /v1/assets/{id}/ingest)
    """
    raw_proxy = await proxy.read()
    if len(raw_proxy) > MAX_PROXY_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Proxy upload too large (10 MB max)")
    if not raw_proxy:
        raise HTTPException(status_code=400, detail="Proxy file is empty")

    tenant_id: str = request.state.tenant_id

    # Validate library
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")

    # Enforce path filters (merged tenant + library)
    filter_repo = PathFilterRepository(session)
    lib_filter_rows = filter_repo.list_for_library(library_id)
    tenant_filter_rows = filter_repo.list_defaults(tenant_id)
    lib_filters = [PathFilter(type=f.type, pattern=f.pattern) for f in lib_filter_rows]
    tenant_filters = [PathFilter(type=f.type, pattern=f.pattern) for f in tenant_filter_rows]
    if (lib_filters or tenant_filters) and not is_path_included_merged(rel_path, tenant_filters, lib_filters):
        raise HTTPException(
            status_code=422,
            detail=f"Path excluded by filters: {rel_path}",
        )

    # Parse optional JSON
    exif_data = _parse_optional_json(exif, "exif")
    vision_data = _parse_optional_json(vision, "vision")
    embeddings_data = _parse_optional_json_list(embeddings, "embeddings")

    # Parse mtime
    file_mtime_dt: datetime | None = None
    if file_mtime:
        try:
            file_mtime_dt = datetime.fromisoformat(file_mtime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid file_mtime format")

    # Create or find existing asset
    asset_repo = AssetRepository(session)
    existing = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
    created = False

    if existing is None:
        # Create a minimal scan record for the asset (required by create_for_scan).
        from src.repository.tenant import ScanRepository
        scan_repo = ScanRepository(session)
        scan = scan_repo.create(library_id=library_id, status="complete")
        asset = asset_repo.create_for_scan(
            library_id=library_id,
            rel_path=rel_path,
            file_size=file_size,
            file_mtime=file_mtime_dt,
            media_type=media_type,
            scan_id=scan.scan_id,
        )
        asset_id = asset.asset_id
        created = True
    else:
        asset_id = existing.asset_id
        # Update file metadata if changed
        existing.file_size = file_size
        if file_mtime_dt is not None:
            existing.file_mtime = file_mtime_dt
        existing.media_type = media_type
        session.add(existing)
        session.commit()

    result = _do_ingest(
        asset_id=asset_id,
        library_id=library_id,
        rel_path=rel_path,
        tenant_id=tenant_id,
        raw_proxy=raw_proxy,
        width=width,
        height=height,
        exif_data=exif_data,
        vision_data=vision_data,
        embeddings_data=embeddings_data,
        session=session,
    )
    result.created = created
    return result


# ---------------------------------------------------------------------------
# POST /v1/assets/{asset_id}/ingest — ingest into existing asset
# ---------------------------------------------------------------------------


@router.post("/v1/assets/{asset_id}/ingest", response_model=IngestResponse)
async def ingest_asset(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    proxy: UploadFile = File(...),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    exif: str | None = Form(default=None),
    vision: str | None = Form(default=None),
    embeddings: str | None = Form(default=None),
) -> IngestResponse:
    """Ingest proxy + metadata into an existing asset record."""
    raw_proxy = await proxy.read()
    if len(raw_proxy) > MAX_PROXY_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Proxy upload too large (10 MB max)")
    if not raw_proxy:
        raise HTTPException(status_code=400, detail="Proxy file is empty")

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    tenant_id: str = request.state.tenant_id

    exif_data = _parse_optional_json(exif, "exif")
    vision_data = _parse_optional_json(vision, "vision")
    embeddings_data = _parse_optional_json_list(embeddings, "embeddings")

    return _do_ingest(
        asset_id=asset_id,
        library_id=asset.library_id,
        rel_path=asset.rel_path,
        tenant_id=tenant_id,
        raw_proxy=raw_proxy,
        width=width,
        height=height,
        exif_data=exif_data,
        vision_data=vision_data,
        embeddings_data=embeddings_data,
        session=session,
    )
