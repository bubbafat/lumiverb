"""Artifact upload endpoint for assets. Requires tenant auth."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository, LibraryRepository
from src.storage.local import LocalStorage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["artifacts"])

ALLOWED_ARTIFACT_TYPES = {"proxy", "thumbnail", "video_preview", "scene_rep"}

# Target limits (not yet enforced per type): proxy ≈ 1 MB, video_preview ≈ 20 MB.
# TODO: enforce per-type limits once remote worker uploads are in place.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB absolute ceiling
UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB read buffer

CONTENT_TYPES: dict[str, str] = {
    "proxy": "image/webp",
    "thumbnail": "image/webp",
    "video_preview": "video/mp4",
    "scene_rep": "image/jpeg",
}


class ArtifactUploadResponse(BaseModel):
    key: str
    sha256: str


@router.post("/{asset_id}/artifacts/{artifact_type}", response_model=ArtifactUploadResponse)
async def upload_artifact(
    asset_id: str,
    artifact_type: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    file: UploadFile = File(...),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    rep_frame_ms: int | None = Form(default=None),
) -> ArtifactUploadResponse:
    """Upload a proxy, thumbnail, video_preview, or scene_rep artifact for an asset.

    Streams the upload to disk in chunks (never fully buffered in memory), computes
    SHA-256 incrementally, and atomic-renames the temp file into place. DB is updated
    after the file is safely on disk.
    """
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )
    if width is not None and (width < 1 or width > 100_000):
        raise HTTPException(status_code=400, detail="width out of range")
    if height is not None and (height < 1 or height > 100_000):
        raise HTTPException(status_code=400, detail="height out of range")
    if file.content_type and artifact_type == "video_preview" and not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="video_preview must be a video file")
    if file.content_type and artifact_type in ("proxy", "thumbnail", "scene_rep") and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"{artifact_type} must be an image file")

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    tenant_id: str = request.state.tenant_id
    storage: LocalStorage = get_storage()

    if artifact_type == "proxy":
        key = storage.proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    elif artifact_type == "thumbnail":
        key = storage.thumbnail_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    elif artifact_type == "video_preview":
        key = storage.video_preview_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    else:  # scene_rep
        if rep_frame_ms is None:
            raise HTTPException(
                status_code=400, detail="rep_frame_ms is required for scene_rep artifacts"
            )
        key = storage.scene_rep_key(tenant_id, asset.library_id, asset_id, rep_frame_ms)

    path = storage.abs_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")

    hasher = hashlib.sha256()
    total_bytes = 0

    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                f.write(chunk)
        tmp_path.rename(path)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Failed to write artifact to storage")

    sha256 = hasher.hexdigest()

    if artifact_type == "proxy":
        asset_repo.set_proxy_artifact(asset_id, key, sha256, width, height)
    elif artifact_type == "thumbnail":
        asset_repo.set_thumbnail_artifact(asset_id, key, sha256)
    else:  # video_preview
        asset_repo.set_video_preview(asset_id, video_preview_key=key)

    return ArtifactUploadResponse(key=key, sha256=sha256)


class BatchArtifactItem(BaseModel):
    artifact_type: str
    key: str
    sha256: str


class BatchArtifactUploadResponse(BaseModel):
    items: list[BatchArtifactItem]


@router.post("/{asset_id}/artifacts", response_model=BatchArtifactUploadResponse)
async def upload_artifacts_batch(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    proxy: UploadFile | None = File(default=None),
    thumbnail: UploadFile | None = File(default=None),
    video_preview: UploadFile | None = File(default=None),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
) -> BatchArtifactUploadResponse:
    """Upload multiple artifacts for an asset in a single request.

    Accepts optional multipart fields: proxy, thumbnail, video_preview.
    Each file is streamed to disk, SHA-256 computed, and DB updated.
    """
    files: dict[str, UploadFile] = {}
    if proxy is not None:
        files["proxy"] = proxy
    if thumbnail is not None:
        files["thumbnail"] = thumbnail
    if video_preview is not None:
        files["video_preview"] = video_preview

    if not files:
        raise HTTPException(status_code=400, detail="No artifact files provided")

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    tenant_id: str = request.state.tenant_id
    storage: LocalStorage = get_storage()
    items: list[BatchArtifactItem] = []

    for artifact_type, upload_file in files.items():
        if artifact_type == "proxy":
            key = storage.proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        elif artifact_type == "thumbnail":
            key = storage.thumbnail_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        elif artifact_type == "video_preview":
            key = storage.video_preview_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported artifact_type in batch: {artifact_type}")

        path = storage.abs_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")

        hasher = hashlib.sha256()
        total_bytes = 0

        try:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await upload_file.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="File too large")
                    hasher.update(chunk)
                    f.write(chunk)
            tmp_path.rename(path)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="Failed to write artifact to storage")

        sha256 = hasher.hexdigest()

        if artifact_type == "proxy":
            asset_repo.set_proxy_artifact(asset_id, key, sha256, width, height)
        elif artifact_type == "thumbnail":
            asset_repo.set_thumbnail_artifact(asset_id, key, sha256)
        elif artifact_type == "video_preview":
            asset_repo.set_video_preview(asset_id, video_preview_key=key)

        items.append(BatchArtifactItem(artifact_type=artifact_type, key=key, sha256=sha256))

    return BatchArtifactUploadResponse(items=items)


@router.get("/{asset_id}/artifacts/{artifact_type}")
def download_artifact(
    asset_id: str,
    artifact_type: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    rep_frame_ms: int | None = Query(default=None),
) -> StreamingResponse:
    """Download a proxy, thumbnail, video_preview, or scene_rep artifact for an asset.

    Returns the raw file bytes with the correct Content-Type. 404 if the artifact
    key is not yet set (artifact_not_ready) or the file is missing on disk (artifact_missing).
    """
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if getattr(request.state, "is_public_request", False):
        public_library_id = request.query_params.get("public_library_id")
        if not public_library_id or asset.library_id != public_library_id:
            raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
        lib = LibraryRepository(session).get_by_id(public_library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")

    if artifact_type == "proxy":
        key = asset.proxy_key
    elif artifact_type == "thumbnail":
        key = asset.thumbnail_key
    elif artifact_type == "video_preview":
        key = asset.video_preview_key
    else:  # scene_rep
        if rep_frame_ms is None:
            raise HTTPException(
                status_code=400, detail="rep_frame_ms is required for scene_rep artifacts"
            )
        tenant_id: str = request.state.tenant_id
        storage: LocalStorage = get_storage()
        key = storage.scene_rep_key(tenant_id, asset.library_id, asset_id, rep_frame_ms)

    if key is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "artifact_not_ready", "message": "Artifact has not been generated yet"},
        )

    storage: LocalStorage = get_storage()
    path = storage.abs_path(key)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail={"code": "artifact_missing", "message": "Artifact file not found on storage"},
        )

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(UPLOAD_CHUNK_SIZE):
                yield chunk

    return StreamingResponse(_iter(), media_type=CONTENT_TYPES[artifact_type])
