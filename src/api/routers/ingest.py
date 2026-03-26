"""Atomic ingest endpoint: upload proxy + optional metadata in one request.

The server normalizes the proxy (WebP, 2048px max), generates a thumbnail
(WebP, 512px), and stores all provided metadata atomically. This is the
primary path for creating fully-populated assets.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from PIL import Image
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core import asset_status
from src.repository.tenant import (
    AssetEmbeddingRepository,
    AssetMetadataRepository,
    AssetRepository,
    SearchSyncQueueRepository,
)
from src.storage.local import LocalStorage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["ingest"])

PROXY_MAX_LONG_EDGE = 2048
THUMBNAIL_LONG_EDGE = 512
WEBP_QUALITY = 80
MAX_PROXY_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB raw upload limit


def _normalize_proxy(data: bytes) -> tuple[bytes, int, int]:
    """Decode image, resize to PROXY_MAX_LONG_EDGE if larger, encode as WebP.

    Returns (webp_bytes, width, height) where width/height are the
    dimensions of the normalized proxy (not the original source).
    """
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")

    w, h = img.size
    long_edge = max(w, h)
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


@router.post("/{asset_id}/ingest", response_model=IngestResponse)
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
    """Atomically ingest an asset with proxy and optional metadata.

    Required:
      - proxy: image file (JPEG, PNG, WebP, etc.) — server normalizes to WebP 2048px.

    Optional form fields (JSON strings):
      - width/height: original source dimensions (passed through to DB).
      - exif: JSON object with EXIF fields (sha256, camera_make, camera_model,
              taken_at, gps_lat, gps_lon, duration_sec, exif).
      - vision: JSON object with AI results (model_id, model_version, description, tags).
      - embeddings: JSON array of {model_id, model_version, vector} objects.

    The server:
      1. Normalizes the proxy to WebP (2048px max long edge, no upscale).
      2. Generates a thumbnail (512px WebP) from the normalized proxy.
      3. Writes both to storage.
      4. Stores EXIF, vision, and embedding data if provided.
      5. Sets asset status based on what was provided.
      6. Enqueues search sync if vision data was provided.
    """
    # --- Read and validate proxy upload ---
    raw_proxy = await proxy.read()
    if len(raw_proxy) > MAX_PROXY_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Proxy upload too large (10 MB max)")
    if not raw_proxy:
        raise HTTPException(status_code=400, detail="Proxy file is empty")

    # --- Validate asset exists ---
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    tenant_id: str = request.state.tenant_id
    storage: LocalStorage = get_storage()

    # --- Parse optional JSON fields ---
    exif_data: dict | None = None
    if exif is not None:
        try:
            exif_data = json.loads(exif)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="exif must be valid JSON")

    vision_data: dict | None = None
    if vision is not None:
        try:
            vision_data = json.loads(vision)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="vision must be valid JSON")

    embeddings_data: list[dict] | None = None
    if embeddings is not None:
        try:
            embeddings_data = json.loads(embeddings)
            if not isinstance(embeddings_data, list):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="embeddings must be a JSON array")

    # --- Normalize proxy to WebP ---
    try:
        proxy_bytes, proxy_w, proxy_h = _normalize_proxy(raw_proxy)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to process proxy image: {e}")

    # --- Generate thumbnail from normalized proxy ---
    thumb_bytes = _generate_thumbnail(proxy_bytes)

    # --- Write proxy to storage ---
    proxy_key = storage.proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    proxy_path = storage.abs_path(proxy_key)
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_path.write_bytes(proxy_bytes)
    proxy_sha256 = hashlib.sha256(proxy_bytes).hexdigest()

    # --- Write thumbnail to storage ---
    thumb_key = storage.thumbnail_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    thumb_path = storage.abs_path(thumb_key)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(thumb_bytes)
    thumb_sha256 = hashlib.sha256(thumb_bytes).hexdigest()

    # --- Update DB: proxy + thumbnail ---
    # Use source dimensions if provided, otherwise use normalized proxy dimensions.
    source_w = width if width is not None else proxy_w
    source_h = height if height is not None else proxy_h
    asset_repo.set_proxy_artifact(asset_id, proxy_key, proxy_sha256, source_w, source_h)
    asset_repo.set_thumbnail_artifact(asset_id, thumb_key, thumb_sha256)

    # Determine final status based on what data was provided.
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
            # Enqueue search sync.
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
