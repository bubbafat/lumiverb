"""Artifact upload endpoint for assets. Requires tenant auth."""

from __future__ import annotations

import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.server.repository.lineage import record as record_lineage

from src.server.api.dependencies import get_tenant_session
from src.server.repository.tenant import AssetRepository, LibraryRepository
from src.server.storage.local import LocalStorage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["artifacts"])

ALLOWED_ARTIFACT_TYPES = {"proxy", "thumbnail", "video_preview", "scene_rep", "analysis_proxy"}

# Target limits (not yet enforced per type): proxy ≈ 1 MB, video_preview ≈ 20 MB.
# TODO: enforce per-type limits once remote worker uploads are in place.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB absolute ceiling
# A full-length analysis proxy of a long recording runs to gigabytes.
MAX_UPLOAD_BYTES_BY_TYPE: dict[str, int] = {"analysis_proxy": 64 * 1024**3}
UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB read buffer


def _max_upload_bytes(artifact_type: str) -> int:
    return MAX_UPLOAD_BYTES_BY_TYPE.get(artifact_type, MAX_UPLOAD_BYTES)

CONTENT_TYPES: dict[str, str] = {
    "proxy": "image/webp",
    "thumbnail": "image/webp",
    "video_preview": "video/mp4",
    "scene_rep": "image/jpeg",
    "analysis_proxy": "video/mp4",
}


def _per_kind(raw: str | None) -> dict:
    """A JSON object of lineage by artifact kind, or {} when absent or unreadable."""
    import json

    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


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
    lineage: str | None = Form(default=None),  # JSON: how it was made (see LineageIn)
) -> ArtifactUploadResponse:
    """Upload a proxy, thumbnail, video_preview, scene_rep or analysis_proxy artifact for an asset.

    analysis_proxy is a video's full-length low-resolution copy with audio,
    for transcription, scenes and vision (ADR-016 phase 2). Streams the upload to disk in chunks (never fully buffered in memory), computes
    SHA-256 incrementally, and atomic-renames the temp file into place. DB is updated
    after the file is safely on disk.
    """
    from src.server.api.routers.producers import require_lineage

    # Proxies, previews and analysis copies say how they were made, before anything is saved.
    made = require_lineage(lineage, artifact_type) if artifact_type in ("proxy", "video_preview", "analysis_proxy") else None
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )
    if width is not None and (width < 1 or width > 100_000):
        raise HTTPException(status_code=400, detail="width out of range")
    if height is not None and (height < 1 or height > 100_000):
        raise HTTPException(status_code=400, detail="height out of range")
    if file.content_type and artifact_type in ("video_preview", "analysis_proxy") and not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail=f"{artifact_type} must be a video file")
    if file.content_type and artifact_type in ("proxy", "thumbnail", "scene_rep") and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"{artifact_type} must be an image file")

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if artifact_type == "analysis_proxy" and asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Analysis proxies are for videos only")

    tenant_id: str = request.state.tenant_id
    storage: LocalStorage = get_storage()
    max_bytes = _max_upload_bytes(artifact_type)

    if artifact_type == "proxy":
        key = storage.proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    elif artifact_type == "thumbnail":
        key = storage.thumbnail_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    elif artifact_type == "video_preview":
        key = storage.video_preview_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
    elif artifact_type == "analysis_proxy":
        key = storage.analysis_proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
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
                if total_bytes > max_bytes:
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
    elif artifact_type == "analysis_proxy":
        asset_repo.set_analysis_proxy(asset_id, key, sha256)
    if artifact_type in ("proxy", "video_preview", "analysis_proxy"):
        record_lineage(session, asset_id, artifact_type, made)
    # scene_rep: no asset-level column to update. The on-disk path is derived
    # from (tenant_id, library_id, asset_id, rep_frame_ms) on download via
    # storage.scene_rep_key(), so the file is fully addressable from
    # video_scenes.rep_frame_ms alone. (Earlier this branch fell through to
    # set_video_preview, which clobbered the MP4 preview path with the JPG.)

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
    # JSON: {"proxy": {...}, "video_preview": {...}}, how each was made (see LineageIn)
    lineage: str | None = Form(default=None),
) -> BatchArtifactUploadResponse:
    """Upload multiple artifacts for an asset in a single request.

    Accepts optional multipart fields: proxy, thumbnail, video_preview.
    Each file is streamed to disk, SHA-256 computed, and DB updated.
    """
    from src.server.api.routers.ingest import _per_kind
    from src.server.api.routers.producers import require_lineage

    # Each proxy or preview sent says how it was made, before anything is saved.
    by_kind = _per_kind(lineage)
    made = {kind: require_lineage(by_kind.get(kind), kind)
            for kind, sent in (("proxy", proxy), ("video_preview", video_preview)) if sent is not None}
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
        if artifact_type in ("proxy", "video_preview"):
            record_lineage(session, asset_id, artifact_type, made[artifact_type])

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
    """Download a proxy, thumbnail, video_preview, scene_rep or analysis_proxy artifact for an asset.

    Returns the raw file bytes with the correct Content-Type. 404 if the artifact
    key is not yet set (artifact_not_ready) or the file is missing on disk (artifact_missing).
    analysis_proxy answers Range requests and carries its SHA-256 as the ETag.
    """
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or (asset.deleted_at is not None and getattr(request.state, "is_public_request", False)):
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
    elif artifact_type == "analysis_proxy":
        # The whole video. Public pages and, while the account caps playback,
        # viewers play it through /playback; editors and the brain's tools need it whole.
        if getattr(request.state, "is_public_request", False):
            raise HTTPException(status_code=403, detail="Analysis proxies aren't public")
        if getattr(request.state, "role", None) == "viewer":
            from src.server.tenant_settings import playback_cap

            if playback_cap(session, public=False) is not None:
                raise HTTPException(status_code=403, detail="Playback is capped for viewers; use /playback")
        key = asset.analysis_proxy_key
    else:  # scene_rep
        if rep_frame_ms is None:
            raise HTTPException(
                status_code=400, detail="rep_frame_ms is required for scene_rep artifacts"
            )
        if getattr(request.state, "is_public_request", False):
            from src.server.tenant_settings import playback_cap

            cap = playback_cap(session, public=True)
            if cap is not None and rep_frame_ms >= cap * 1000:
                raise HTTPException(status_code=403, detail="Past what public pages play")
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

    if artifact_type == "analysis_proxy":
        from src.server.api.routers.assets import _stream_file_with_range

        etag = f'"{asset.analysis_proxy_sha256}"' if asset.analysis_proxy_sha256 else None
        return _stream_file_with_range(path, request, media_type=CONTENT_TYPES[artifact_type], etag=etag)

    if artifact_type == "video_preview":
        # Within the playback cap for whoever is asking, like /preview.
        from src.server.api.routers.assets import _stream_file_with_range
        from src.server.api.routers.playback import capped
        from src.server.tenant_settings import playback_cap

        st = path.stat()
        path = capped(
            path, playback_cap(session, public=getattr(request.state, "is_public_request", False)),
            storage=storage, tenant_id=request.state.tenant_id, asset_id=asset_id, source="preview",
            version=f"{int(st.st_mtime)}-{st.st_size}", strip=getattr(request.state, "is_public_request", False),
        )
        return _stream_file_with_range(path, request, media_type=CONTENT_TYPES[artifact_type])

    def _iter():
        with open(path, "rb") as f:
            while chunk := f.read(UPLOAD_CHUNK_SIZE):
                yield chunk

    return StreamingResponse(_iter(), media_type=CONTENT_TYPES[artifact_type])
