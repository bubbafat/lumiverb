"""Artifact upload endpoint for assets. Requires tenant auth."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository
from src.storage.local import LocalStorage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["artifacts"])

ALLOWED_ARTIFACT_TYPES = {"proxy", "thumbnail", "video_preview"}

# Target limits (not yet enforced per type): proxy ≈ 1 MB, video_preview ≈ 20 MB.
# TODO: enforce per-type limits once remote worker uploads are in place.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB absolute ceiling
UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB read buffer


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
) -> ArtifactUploadResponse:
    """Upload a proxy, thumbnail, or video_preview artifact for an asset.

    Streams the upload to disk in chunks (never fully buffered in memory), computes
    SHA-256 incrementally, and atomic-renames the temp file into place. DB is updated
    after the file is safely on disk.
    """
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )

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
    else:  # video_preview
        key = storage.video_preview_key(tenant_id, asset.library_id, asset_id, asset.rel_path)

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
