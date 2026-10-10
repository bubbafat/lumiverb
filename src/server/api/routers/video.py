"""Video chunk API: init chunks, claim next, complete, fail. All require tenant auth."""

import json
import uuid
from typing import Any, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, select

from src.server.api.dependencies import get_tenant_session, require_editor
from src.server.api.errors import ConflictError
from src.server.api.routers.producers import LineageIn, require_lineage
from src.server.repository.lineage import record as record_lineage
from src.server.models.tenant import VideoIndexChunk
from src.server.repository.tenant import (
    AssetMetadataRepository,
    ChunksBusy,
    AssetRepository,
    VideoIndexChunkRepository,
    VideoSceneRepository,
)
from src.server.storage.local import get_storage

import logging
_log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/video", tags=["video"])


# ---------------------------------------------------------------------------
# Init chunks
# ---------------------------------------------------------------------------


class InitChunksRequest(BaseModel):
    duration_sec: float
    # The clip's scenes were found another way than lineage says (a redo):
    # start it over. Its scenes go, with their descriptions, images and
    # search entries; scenes hold no human data.
    redo: bool = False
    lineage: Any = None  # how they'll be found (LineageIn), when redo


class InitChunksResponse(BaseModel):
    chunk_count: int
    already_initialized: bool


@router.post("/{asset_id}/chunks", response_model=InitChunksResponse, dependencies=[Depends(require_editor)])
def init_chunks(
    asset_id: str,
    body: InitChunksRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> InitChunksResponse:
    """The clip's chunks, made once (idempotent). With redo: started over
    first when its scenes were found another way than lineage says (422
    lineage_required when it doesn't say); a redo sent again, once they're
    being found or were found that way, changes nothing."""
    if body.redo:
        made = require_lineage(body.lineage, "scenes")
        _now_current(session, made)
        _start_over(request, session, asset_id, made)
    chunk_repo = VideoIndexChunkRepository(session)
    created = chunk_repo.create_chunks_for_asset(asset_id, body.duration_sec)
    # A new run: chunks that failed in an earlier one are tried again (within
    # a run a failed chunk stays failed, so one bad chunk can't loop it).
    chunk_repo.retry_failed(asset_id)
    total = chunk_repo.chunk_count(asset_id)
    return InitChunksResponse(
        chunk_count=total,
        already_initialized=created == 0,
    )


def _now_current(session: Session, made: dict) -> None:
    """409 settings_changed unless made is how scenes are found now: a redo
    with settings read before they changed again would record stale ones."""
    from src.server.repository import lineage

    want = lineage.desired(session, "scenes")
    if (made["producer"], str(made.get("version") or ""), made.get("settings_hash")) != (
            want["producer"], want["version"], want["settings_hash"]):
        raise ConflictError("settings_changed", "Scenes are found with other settings now: read them again.",
                            {"settings_hash": want["settings_hash"]})


def _start_over(request: Request, session: Session, asset_id: str, made: dict) -> None:
    """Drop a clip's scenes found another way than made says, so they're
    found again: its scenes, chunks and their lineage (scenes and scene
    descriptions), then their images and search entries. Nothing when the
    clip has no scenes found (under way, or never) or found them this way."""
    from src.server.repository import lineage

    row = session.execute(text(
        "SELECT a.library_id, a.video_indexed, l.producer, l.producer_version, l.settings_hash"
        " FROM assets a LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = 'scenes'"
        " WHERE a.asset_id = :a FOR UPDATE OF a"), {"a": asset_id}).first()
    if row is None or not row.video_indexed:
        return
    if (row.producer, row.producer_version, row.settings_hash) == (
            made["producer"], str(made.get("version") or ""), made.get("settings_hash")):
        return
    frames = [r[0] for r in session.execute(text("SELECT rep_frame_ms FROM video_scenes WHERE asset_id = :a"),
                                            {"a": asset_id})]
    session.execute(text("DELETE FROM video_scenes WHERE asset_id = :a"), {"a": asset_id})
    session.execute(text("DELETE FROM video_index_chunks WHERE asset_id = :a"), {"a": asset_id})
    lineage.start_over(session, [asset_id], "scenes")  # and what's made from them (its redo_also)
    session.execute(text("UPDATE assets SET video_indexed = false WHERE asset_id = :a"), {"a": asset_id})
    session.commit()
    _log.info("Scenes of %s found again: %d old ones dropped", asset_id, len(frames))
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return
    from src.server.search.quickwit_client import QuickwitClient

    storage = get_storage()
    # Only the images at the keys the server derives (a scene's rep frame).
    for ms in frames:  # best effort: an image left behind is only space
        try:
            storage.abs_path(storage.scene_rep_key(tenant_id, row.library_id, asset_id, ms)).unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("Couldn't delete a scene image of %s: %s", asset_id, exc)
    QuickwitClient().delete_scene_index_documents_by_asset_ids(tenant_id, [asset_id])


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


@router.post("/{asset_id}/chunks/next", response_model=None, dependencies=[Depends(require_editor)])
def claim_next_chunk(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    request: Request,
) -> Response | ChunkWorkOrder:
    worker_id = f"vid_{uuid.uuid4().hex[:12]}"
    chunk_repo = VideoIndexChunkRepository(session)
    asset_repo = AssetRepository(session)

    try:
        chunk = chunk_repo.claim_next_chunk(asset_id, worker_id)
    except ChunksBusy as e:
        raise ConflictError("chunks_busy", f"Another chunk of this clip is under way or failed ({e}).") from e
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
    # The server numbers them (scene_index), after the clip's scenes already stored.
    start_ms: int
    end_ms: int
    rep_frame_ms: int
    rep_frame_sha256: str | None = None
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
    # How the scenes were found; recorded when the clip's last chunk completes.
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class ChunkCompleteResponse(BaseModel):
    chunk_id: str
    scenes_saved: int
    all_complete: bool


@router.post("/chunks/{chunk_id}/complete", response_model=ChunkCompleteResponse, dependencies=[Depends(require_editor)])
def complete_chunk(
    chunk_id: str,
    body: ChunkCompleteRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ChunkCompleteResponse:
    made = require_lineage(body.lineage, "scenes")  # how the scenes were found, before anything is saved
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
        # How they were found lands with them: a redo in between sees both or neither.
        record_lineage(session, asset_id, "scenes", made, commit=False)
        asset_repo.set_video_indexed(asset_id)
        session.commit()
        # Inline search sync (best-effort)
        asset_obj = asset_repo.get_by_id(asset_id)
        if asset_obj:
            meta_obj = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
            if meta_obj:
                from src.server.search.sync import try_sync_asset
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


@router.post("/chunks/{chunk_id}/fail", dependencies=[Depends(require_editor)])
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
    # How its description was made ({producer, version, settings_hash}); null
    # when it has none, or one made without saying. The worker describes a
    # scene again when this isn't what it would record.
    lineage: dict | None = None


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
                lineage=s.lineage,
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
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class SceneVisionUpdateResponse(BaseModel):
    scene_id: str
    status: str


@router.patch("/scenes/{scene_id}", response_model=SceneVisionUpdateResponse, dependencies=[Depends(require_editor)])
def update_scene_vision(
    scene_id: str,
    body: SceneVisionUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> SceneVisionUpdateResponse:
    """Update vision results on a scene after describing its rep frame; it
    says how (422 lineage_required, before anything is saved)."""
    made = require_lineage(body.lineage, "scene_vision")
    scene_repo = VideoSceneRepository(session)
    try:
        scene_repo.update_vision(
            scene_id=scene_id,
            model_id=body.model_id,
            model_version=body.model_version,
            description=body.description,
            tags=body.tags,
            lineage=_scene_lineage(made),
        )
    except (ValueError, StaleDataError) as exc:  # its clip's scenes were found again meanwhile
        if isinstance(exc, ValueError) and not str(exc).startswith("Scene not found"):
            raise
        session.rollback()
        raise _scene_gone(scene_id) from None
    # One record for the clip's scene descriptions, made once every scene's
    # description was made the same way: a video described half with old
    # settings and half with new isn't current, and its redo isn't done.
    asset_id = session.execute(text("SELECT asset_id FROM video_scenes WHERE scene_id = :s"),
                               {"s": scene_id}).scalar()
    if asset_id:
        others = session.execute(text(
            "SELECT count(*) FROM video_scenes WHERE asset_id = :a"
            " AND (description IS NULL OR lineage IS DISTINCT FROM CAST(:l AS jsonb))"
        ), {"a": asset_id, "l": json.dumps(_scene_lineage(made))}).scalar()
        if not others:
            record_lineage(session, asset_id, "scene_vision", made)
    return SceneVisionUpdateResponse(scene_id=scene_id, status="updated")


def _scene_gone(scene_id: str) -> ConflictError:
    """409 scene_gone: the scene was dropped (its video's scenes were found
    again); the new ones are described next, so it's no failure."""
    return ConflictError("scene_gone", "This scene is gone: its video's scenes were found again.",
                         {"scene_id": scene_id})


def _scene_lineage(made: dict | None) -> dict:
    """A scene's own record of how its description was made (as the clip's
    is recorded: a write that doesn't say, or names another producer, is unknown's)."""
    from src.shared.producers import PRODUCERS, UNKNOWN

    made = made or {}
    if made.get("producer") != PRODUCERS["scene_vision"].producer:
        return {"producer": UNKNOWN, "version": "", "settings_hash": ""}
    return {"producer": made["producer"], "version": str(made.get("version") or ""),
            "settings_hash": str(made.get("settings_hash") or "")}


# ---------------------------------------------------------------------------
# Enqueue scene-level search sync # ---------------------------------------------------------------------------


class SceneSyncRequest(BaseModel):
    asset_id: str


@router.post("/scenes/{scene_id}/sync", dependencies=[Depends(require_editor)])
def sync_scene(
    scene_id: str,
    body: SceneSyncRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Sync a scene to Quickwit search index."""
    from src.server.search.sync import try_sync_scene
    scene = VideoSceneRepository(session).get_by_id(scene_id)
    if scene is None:
        raise _scene_gone(scene_id)
    asset = AssetRepository(session).get_by_id(body.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    ok = try_sync_scene(session, scene, asset, tenant_id=getattr(request.state, "tenant_id", None))
    return {"scene_id": scene_id, "status": "synced" if ok else "deferred"}
