"""Video chunk API: init chunks, claim next, complete, fail. All require tenant auth."""

import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from src.api.dependencies import get_tenant_session
from src.models.tenant import VideoIndexChunk
from src.repository.tenant import (
    AssetMetadataRepository,
    AssetRepository,
    VideoIndexChunkRepository,
    VideoSceneRepository,
)

import logging
_log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/video", tags=["video"])


# ---------------------------------------------------------------------------
# Init chunks
# ---------------------------------------------------------------------------


class InitChunksRequest(BaseModel):
    duration_sec: float


class InitChunksResponse(BaseModel):
    chunk_count: int
    already_initialized: bool


@router.post("/{asset_id}/chunks", response_model=InitChunksResponse)
def init_chunks(
    asset_id: str,
    body: InitChunksRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> InitChunksResponse:
    chunk_repo = VideoIndexChunkRepository(session)
    created = chunk_repo.create_chunks_for_asset(asset_id, body.duration_sec)
    total = chunk_repo.chunk_count(asset_id)
    return InitChunksResponse(
        chunk_count=total,
        already_initialized=created == 0,
    )


# ---------------------------------------------------------------------------
# Claim next chunk
# ---------------------------------------------------------------------------


class ChunkWorkOrder(BaseModel):
    chunk_id: str
    worker_id: str
    chunk_index: int
    start_ts: float
    end_ts: float
    overlap_sec: float
    anchor_phash: str | None
    scene_start_ts: float | None
    video_duration_sec: float
    is_last: bool


@router.get("/{asset_id}/chunks/next", response_model=None)
def claim_next_chunk(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    request: Request,
) -> Response | ChunkWorkOrder:
    worker_id = f"vid_{uuid.uuid4().hex[:12]}"
    chunk_repo = VideoIndexChunkRepository(session)
    asset_repo = AssetRepository(session)

    chunk = chunk_repo.claim_next_chunk(asset_id, worker_id)
    if chunk is None:
        return Response(status_code=204)

    asset = asset_repo.get_by_id(asset_id)
    total_chunks = chunk_repo.chunk_count(asset_id)
    video_duration_sec = asset.duration_sec if asset and asset.duration_sec is not None else 0.0

    return ChunkWorkOrder(
        chunk_id=chunk.chunk_id,
        worker_id=worker_id,
        chunk_index=chunk.chunk_index,
        start_ts=chunk.start_ms / 1000.0,
        end_ts=chunk.end_ms / 1000.0,
        overlap_sec=VideoIndexChunkRepository.OVERLAP_SEC,
        anchor_phash=chunk.anchor_phash,
        scene_start_ts=chunk.scene_start_ms / 1000.0 if chunk.scene_start_ms is not None else None,
        video_duration_sec=video_duration_sec,
        is_last=(chunk.chunk_index == total_chunks - 1),
    )


# ---------------------------------------------------------------------------
# Complete chunk
# ---------------------------------------------------------------------------


class SceneResult(BaseModel):
    scene_index: int
    start_ms: int
    end_ms: int
    rep_frame_ms: int
    rep_frame_sha256: str | None = None
    proxy_key: str | None = None
    thumbnail_key: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    sharpness_score: float | None = None
    keep_reason: str | None = None
    phash: str | None = None


class ChunkCompleteRequest(BaseModel):
    worker_id: str
    scenes: list[SceneResult]
    next_anchor_phash: str | None
    next_scene_start_ms: int | None


class ChunkCompleteResponse(BaseModel):
    chunk_id: str
    scenes_saved: int
    all_complete: bool


@router.post("/chunks/{chunk_id}/complete", response_model=ChunkCompleteResponse)
def complete_chunk(
    chunk_id: str,
    body: ChunkCompleteRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ChunkCompleteResponse:
    chunk_repo = VideoIndexChunkRepository(session)
    chunk = session.exec(select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)).first()
    if chunk is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    ok = chunk_repo.complete_chunk(
        chunk_id=chunk_id,
        worker_id=body.worker_id,
        next_anchor_phash=body.next_anchor_phash,
        next_scene_start_ms=body.next_scene_start_ms,
        scenes=[s.model_dump() for s in body.scenes],
    )
    if not ok:
        raise HTTPException(status_code=409, detail="Chunk not claimable by this worker")

    asset_id = chunk.asset_id
    all_done = chunk_repo.all_chunks_complete(asset_id)

    if all_done and asset_id:
        asset_repo = AssetRepository(session)
        asset_repo.set_video_indexed(asset_id)
        session.commit()
        # Inline search sync (best-effort)
        asset_obj = asset_repo.get_by_id(asset_id)
        if asset_obj:
            meta_obj = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
            if meta_obj:
                from src.search.sync import try_sync_asset
                try_sync_asset(session, asset_obj, meta_obj, tenant_id=getattr(request.state, "tenant_id", None))

    return ChunkCompleteResponse(
        chunk_id=chunk_id,
        scenes_saved=len(body.scenes),
        all_complete=all_done,
    )


# ---------------------------------------------------------------------------
# Fail chunk
# ---------------------------------------------------------------------------


class ChunkFailRequest(BaseModel):
    worker_id: str
    error_message: str


@router.post("/chunks/{chunk_id}/fail")
def fail_chunk(
    chunk_id: str,
    body: ChunkFailRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    chunk_repo = VideoIndexChunkRepository(session)
    ok = chunk_repo.fail_chunk(chunk_id, body.worker_id, body.error_message)
    if not ok:
        raise HTTPException(status_code=409, detail="Chunk not owned by this worker")
    return {"chunk_id": chunk_id, "status": "failed"}


# ---------------------------------------------------------------------------
# List scenes # ---------------------------------------------------------------------------


class SceneListItem(BaseModel):
    scene_id: str
    start_ms: int
    end_ms: int
    rep_frame_ms: int
    thumbnail_key: str | None
    description: str | None
    tags: list[str] | None
    sharpness_score: float | None
    keep_reason: str | None
    phash: str | None


class ScenesResponse(BaseModel):
    scenes: list[SceneListItem]


@router.get("/{asset_id}/scenes", response_model=ScenesResponse)
def get_scenes_for_asset(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ScenesResponse:
    """Return all scenes for an asset ordered by start_ms."""
    scene_repo = VideoSceneRepository(session)
    scenes = scene_repo.get_scenes_for_asset(asset_id)
    return ScenesResponse(
        scenes=[
            SceneListItem(
                scene_id=s.scene_id,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                rep_frame_ms=s.rep_frame_ms,
                thumbnail_key=s.thumbnail_key,
                description=s.description,
                tags=s.tags,
                sharpness_score=s.sharpness_score,
                keep_reason=s.keep_reason,
                phash=s.phash,
            )
            for s in scenes
        ]
    )


# ---------------------------------------------------------------------------
# Update scene vision # ---------------------------------------------------------------------------


class SceneVisionUpdateRequest(BaseModel):
    model_id: str
    model_version: str
    description: str
    tags: list[str]


class SceneVisionUpdateResponse(BaseModel):
    scene_id: str
    status: str


@router.patch("/scenes/{scene_id}", response_model=SceneVisionUpdateResponse)
def update_scene_vision(
    scene_id: str,
    body: SceneVisionUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> SceneVisionUpdateResponse:
    """Update vision results on a scene after describing its rep frame."""
    scene_repo = VideoSceneRepository(session)
    scene_repo.update_vision(
        scene_id=scene_id,
        model_id=body.model_id,
        model_version=body.model_version,
        description=body.description,
        tags=body.tags,
    )
    return SceneVisionUpdateResponse(scene_id=scene_id, status="updated")


# ---------------------------------------------------------------------------
# Enqueue scene-level search sync # ---------------------------------------------------------------------------


class SceneSyncRequest(BaseModel):
    asset_id: str


@router.post("/scenes/{scene_id}/sync")
def sync_scene(
    scene_id: str,
    body: SceneSyncRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Sync a scene to Quickwit search index."""
    from src.search.sync import try_sync_scene
    scene = VideoSceneRepository(session).get_by_id(scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail="Scene not found")
    asset = AssetRepository(session).get_by_id(body.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    ok = try_sync_scene(session, scene, asset, tenant_id=getattr(request.state, "tenant_id", None))
    return {"scene_id": scene_id, "status": "synced" if ok else "deferred"}


# ---------------------------------------------------------------------------
# Reset video pipeline for a library
# ---------------------------------------------------------------------------


class VideoResetResponse(BaseModel):
    library_id: str
    scenes_deleted: int
    chunks_deleted: int
    assets_reset: int
    quickwit_index_deleted: bool
    scene_files_deleted: int


@router.post("/reset", response_model=VideoResetResponse)
def reset_video_pipeline(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> VideoResetResponse:
    """
    Reset the video indexing pipeline for a library.
    Deletes all scenes, chunks, and rep-frame files; clears the Quickwit scene
    index; clears video_indexed on all video assets.
    After this, re-enqueue video-index to reprocess from scratch.
    """
    from src.core.config import get_settings
    from src.search.quickwit_client import QuickwitClient

    scene_repo = VideoSceneRepository(session)
    chunk_repo = VideoIndexChunkRepository(session)
    asset_repo = AssetRepository(session)

    scenes_deleted = scene_repo.delete_for_library(library_id)
    chunks_deleted = chunk_repo.delete_for_library(library_id)
    assets_reset = asset_repo.reset_video_indexed_for_library(library_id)

    # Delete the Quickwit scene index (recreated on next search-sync run).
    quickwit_index_deleted = QuickwitClient().delete_scene_index_for_library(library_id)

    # Delete scene rep-frame files from the data dir.
    tenant_id = getattr(request.state, "tenant_id", None)
    scene_files_deleted = 0
    if tenant_id:
        settings = get_settings()
        scenes_dir = Path(settings.data_dir) / tenant_id / library_id / "scenes"
        if scenes_dir.exists():
            files = list(scenes_dir.rglob("*.jpg"))
            scene_files_deleted = len(files)
            shutil.rmtree(scenes_dir, ignore_errors=True)

    _log.info(
        "Video pipeline reset for library_id=%s: scenes=%d chunks=%d assets=%d "
        "quickwit=%s files=%d",
        library_id, scenes_deleted, chunks_deleted, assets_reset,
        quickwit_index_deleted, scene_files_deleted,
    )
    return VideoResetResponse(
        library_id=library_id,
        scenes_deleted=scenes_deleted,
        chunks_deleted=chunks_deleted,
        assets_reset=assets_reset,
        quickwit_index_deleted=quickwit_index_deleted,
        scene_files_deleted=scene_files_deleted,
    )
