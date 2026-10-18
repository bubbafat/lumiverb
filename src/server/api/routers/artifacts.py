"""Artifact upload endpoint for assets. Requires tenant auth."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor
from src.server.api.devices import may_call
from src.server.api.errors import ApiError
from src.server.repository.assets import AssetRepository
from src.server.repository.lineage import record as record_lineage
from src.server.storage import store as keys
from src.server.storage.store import NotFound, Store, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/assets", tags=["artifacts"])

ALLOWED_ARTIFACT_TYPES = {"proxy", "thumbnail", "video_preview", "scene_rep", "analysis_proxy"}
# What a library's reader makes from the originals and sends: a device sends only these.
READER_ARTIFACTS = ("proxy", "thumbnail", "video_preview", "analysis_proxy")

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


# Videos are kept checked and rewritten (src/server/uploaded_video.py): what a browser plays, without metadata.
VIDEO_ARTIFACTS = frozenset({"video_preview", "analysis_proxy"})

# Image artifacts are stored re-encoded, without the original's metadata
# (src/server/preview_image.py): (format, quality). A preview's and a
# thumbnail's quality is the account's (preview_image.quality), this its default.
IMAGE_ARTIFACTS: dict[str, tuple[str, int]] = {"proxy": ("WEBP", 80), "thumbnail": ("WEBP", 80),
                                                "scene_rep": ("JPEG", 90)}
PREVIEW_IMAGES = ("proxy", "thumbnail")


def _reencode_image_file(path: Path, fmt: str, quality: int) -> str:
    """Rewrites the uploaded image at `path` in place; its new sha256."""
    from src.server.preview_image import reencoded

    data, _, _ = reencoded(path.read_bytes(), fmt=fmt, quality=quality)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


async def _save_upload(store: Store, key: str, upload: UploadFile, artifact_type: str,
                       session: Session | None = None) -> tuple[str, float | None]:
    """The upload, kept under key: streamed (never all in memory) to a write
    nothing sees until it's whole, an image re-encoded (IMAGE_ARTIFACTS; a
    preview and a thumbnail at the account's quality, given its session), a
    video checked and rewritten (VIDEO_ARTIFACTS), then put in its place. Its
    SHA-256 as kept, and a video's length as the server measured it. 413 past
    the type's limit, 400 for an image that isn't one, 422 video_not_playable
    for a video that isn't one Lumiverb keeps; nothing is kept then."""
    from starlette.concurrency import run_in_threadpool

    from src.server import uploaded_video
    from src.server.preview_image import NotAnImageError

    max_bytes = _max_upload_bytes(artifact_type)
    hasher = hashlib.sha256()
    total = 0
    with store.writing(key, suffix=Path(key).suffix) as partial:
        with open(partial, "wb") as f:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="File too large")
                hasher.update(chunk)
                f.write(chunk)
        if artifact_type in VIDEO_ARTIFACTS:
            try:
                kept = await run_in_threadpool(uploaded_video.keep, partial)
            except uploaded_video.NotPlayableError as exc:
                raise ApiError(422, "video_not_playable",
                               f"{artifact_type} isn't a video every browser plays: {exc}. Send H.264 (8-bit 4:2:0,"
                               " at most High profile, limited range) with AAC-LC audio, at most stereo, in an MP4.",
                               {"problems": exc.problems}) from None
            return kept.sha256, kept.duration_sec
        if artifact_type not in IMAGE_ARTIFACTS:
            return hasher.hexdigest(), None
        fmt, quality = IMAGE_ARTIFACTS[artifact_type]
        if artifact_type in PREVIEW_IMAGES and session is not None:
            from src.server.preview_image import quality as account_quality

            quality = account_quality(session)
        try:
            return await run_in_threadpool(_reencode_image_file, partial, fmt, quality), None
        except NotAnImageError:
            raise HTTPException(status_code=400, detail=f"{artifact_type} must be a JPEG, PNG or WebP image") from None


def artifact_key(tenant_id: str, asset, artifact_type: str) -> str:
    """Where a clip's proxy, thumbnail, video preview or analysis proxy is kept (scene frames go by their time)."""
    if artifact_type == "proxy":
        return keys.proxy_key(tenant_id, asset.library_id, asset.asset_id, asset.rel_path)
    if artifact_type == "thumbnail":
        return keys.thumbnail_key(tenant_id, asset.library_id, asset.asset_id, asset.rel_path)
    if artifact_type == "video_preview":
        return keys.video_preview_key(tenant_id, asset.library_id, asset.asset_id, asset.rel_path)
    if artifact_type == "analysis_proxy":
        return keys.analysis_proxy_key(tenant_id, asset.library_id, asset.asset_id, asset.rel_path)
    raise ValueError(artifact_type)


def record_kept(session: Session, asset_id: str, artifact_type: str, key: str, sha256: str, made: dict | None,
                width: int | None = None, height: int | None = None) -> None:
    """A kept artifact on its clip, with how it was made (a proxy, video preview or analysis proxy)."""
    asset_repo = AssetRepository(session)
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


class ArtifactUploadResponse(BaseModel):
    key: str
    sha256: str  # of what's kept: a video's rewrite, an image's re-encoding
    duration_sec: float | None = None  # a video's length, as the server measured it


@router.post("/{asset_id}/artifacts/{artifact_type}", response_model=ArtifactUploadResponse, dependencies=[Depends(require_editor)])
@may_call("asset", artifacts=READER_ARTIFACTS)
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
    for transcription, scenes and vision (ADR-016 phase 2). Kept in the
    Store (_save_upload); the database is updated once it's kept whole.
    """
    from src.server.api.routers.producers import require_lineage

    # Proxies, previews and analysis copies say how they were made, before
    # anything is saved; a thumbnail as the proxy's (one producer makes both).
    made = require_lineage(lineage, artifact_type) if artifact_type in ("proxy", "video_preview", "analysis_proxy") else None
    if artifact_type == "thumbnail":
        require_lineage(lineage, "proxy")
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
    if artifact_type != "scene_rep":
        key = artifact_key(tenant_id, asset, artifact_type)
    else:
        if rep_frame_ms is None:
            raise HTTPException(
                status_code=400, detail="rep_frame_ms is required for scene_rep artifacts"
            )
        key = keys.scene_rep_key(tenant_id, asset.library_id, asset_id, rep_frame_ms)

    sha256, duration_sec = await _save_upload(get_store(), key, file, artifact_type, session)
    record_kept(session, asset_id, artifact_type, key, sha256, made, width, height)
    # scene_rep: no asset-level column to update. The on-disk path is derived
    # from (tenant_id, library_id, asset_id, rep_frame_ms) on download via
    # storage.scene_rep_key(), so the file is fully addressable from
    # video_scenes.rep_frame_ms alone. (Earlier this branch fell through to
    # set_video_preview, which clobbered the MP4 preview path with the JPG.)

    return ArtifactUploadResponse(key=key, sha256=sha256, duration_sec=duration_sec)


class BatchArtifactItem(BaseModel):
    artifact_type: str
    key: str
    sha256: str
    duration_sec: float | None = None


class BatchArtifactUploadResponse(BaseModel):
    items: list[BatchArtifactItem]


@router.post("/{asset_id}/artifacts", response_model=BatchArtifactUploadResponse, dependencies=[Depends(require_editor)])
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
    Each is kept as a single upload keeps it (_save_upload: a proxy or
    thumbnail re-encoded), then the database updated.
    """
    from src.server.api.routers.ingest import _per_kind
    from src.server.api.routers.producers import require_lineage

    # Each proxy or preview sent says how it was made, before anything is saved.
    by_kind = _per_kind(lineage)
    made = {kind: require_lineage(by_kind.get(kind), kind)
            for kind, sent in (("proxy", proxy), ("video_preview", video_preview)) if sent is not None}
    if thumbnail is not None:  # a thumbnail is the proxy producer's too
        require_lineage(by_kind.get("proxy"), "proxy")
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
    store = get_store()
    items: list[BatchArtifactItem] = []

    for artifact_type, upload_file in files.items():
        if artifact_type == "proxy":
            key = keys.proxy_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        elif artifact_type == "thumbnail":
            key = keys.thumbnail_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        elif artifact_type == "video_preview":
            key = keys.video_preview_key(tenant_id, asset.library_id, asset_id, asset.rel_path)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported artifact_type in batch: {artifact_type}")

        sha256, duration_sec = await _save_upload(store, key, upload_file, artifact_type, session)

        if artifact_type == "proxy":
            asset_repo.set_proxy_artifact(asset_id, key, sha256, width, height)
        elif artifact_type == "thumbnail":
            asset_repo.set_thumbnail_artifact(asset_id, key, sha256)
        elif artifact_type == "video_preview":
            asset_repo.set_video_preview(asset_id, video_preview_key=key)
        if artifact_type in ("proxy", "video_preview"):
            record_lineage(session, asset_id, artifact_type, made[artifact_type])

        items.append(BatchArtifactItem(artifact_type=artifact_type, key=key, sha256=sha256, duration_sec=duration_sec))

    return BatchArtifactUploadResponse(items=items)


@router.get("/{asset_id}/artifacts/{artifact_type}")
def download_artifact(
    asset_id: str,
    artifact_type: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    rep_frame_ms: int | None = Query(default=None),
) -> StreamingResponse:
    """A clip's proxy, thumbnail, video_preview, scene_rep or analysis_proxy:
    the one route for each of them, for every client.

    Signed in, a clip out of sight (archived, in the trash) still shows its
    pictures: the archive and trash views need them. A public page's visitor
    (public_library_id or public_project_id) sees only that page's clips in
    sight, never an analysis proxy, and video within the public cap.

    404 artifact_not_ready when it hasn't been made; 404 artifact_missing
    when its file is gone, which is reported for upkeep to repair (the GET
    changes nothing). video_preview within the playback cap: 503
    playback_preparing (Retry-After) while the capped copy is made.
    analysis_proxy answers Range requests and carries its SHA-256 as the ETag.
    """
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"artifact_type must be one of: {', '.join(sorted(ALLOWED_ARTIFACT_TYPES))}",
        )

    public = getattr(request.state, "is_public_request", False)
    asset = AssetRepository(session).get_any(asset_id)  # out of sight too: signed in, those show
    if asset is None or (asset.deleted_at is not None and public):
        raise HTTPException(status_code=404, detail="Asset not found")
    if public:
        from src.server.api.routers.playback import _check_public

        public_library_id = request.query_params.get("public_library_id")
        public_project_id = request.query_params.get("public_project_id")
        if public_library_id and public_project_id:
            raise HTTPException(status_code=400, detail="Give a public library or a public project, not both")
        _check_public(session, asset, public_library_id, public_project_id)

    if artifact_type == "proxy":
        key = asset.proxy_key
    elif artifact_type == "thumbnail":
        key = asset.thumbnail_key
    elif artifact_type == "video_preview":
        key = asset.video_preview_key
    elif artifact_type == "analysis_proxy":
        # The whole video. Public pages and, while the account caps playback,
        # viewers play it through /playback; editors and the server's tools need it whole.
        if public:
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
        if public:
            from src.server.tenant_settings import playback_cap

            cap = playback_cap(session, public=True)
            if cap is not None and rep_frame_ms >= cap * 1000:
                raise HTTPException(status_code=403, detail="Past what public pages play")
        key = keys.scene_rep_key(request.state.tenant_id, asset.library_id, asset_id, rep_frame_ms)

    if key is None:
        raise ApiError(404, "artifact_not_ready", "Artifact has not been generated yet")

    from src.server.api.streaming import stream_stored

    store = get_store()
    media_type = CONTENT_TYPES[artifact_type]
    if artifact_type in ("proxy", "thumbnail"):
        media_type = _IMAGE_TYPES.get(Path(key).suffix.lower(), media_type)
    etag = None
    try:
        if artifact_type == "analysis_proxy" and asset.analysis_proxy_sha256:
            etag = f'"{asset.analysis_proxy_sha256}"'
        if artifact_type == "video_preview":
            # Within the playback cap for whoever is asking, like /playback.
            from src.server.api.routers.playback import capped
            from src.server.tenant_settings import playback_cap

            st = store.stat(key)
            store, key = capped(
                store, key, playback_cap(session, public=public),
                tenant_id=request.state.tenant_id, asset_id=asset_id, source="preview",
                version=f"{int(st.modified)}-{st.size}", strip=public,
            )
        return stream_stored(store, key, request, media_type=media_type, etag=etag)
    except NotFound:
        from src.server import missing_artifacts

        missing_artifacts.report(session, asset_id, artifact_type)
        raise ApiError(404, "artifact_missing", "Artifact file not found on storage") from None


# Older proxies and thumbnails are JPEGs.
_IMAGE_TYPES = {".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
