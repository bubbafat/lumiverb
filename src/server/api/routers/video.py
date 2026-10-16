"""Video API: scene chunks (init, claim next, complete, fail), scenes, and a
video's faces. All require tenant auth."""

import json
import uuid
from typing import Any, Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, select

from src.server.after_commit import AfterCommit
from src.server.api.content_hashes import why_not
from src.server.api.dependencies import get_tenant_session, require_editor
from src.server.api.errors import ApiError, ConflictError, GoneError
from src.server.api.routers.producers import LineageIn, require_lineage, with_source
from src.server.repository.lineage import record as record_lineage
from src.server.models.tenant import VideoIndexChunk
from src.server.repository.tenant import (
    AssetMetadataRepository,
    ChunksBusy,
    AssetRepository,
    FaceRepository,
    LibraryRepository,
    VideoIndexChunkRepository,
    VideoSceneRepository,
)
from src.server.storage import store as stored
from src.server.storage.store import get_store

import logging
_log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/video", tags=["video"])


# ---------------------------------------------------------------------------
# Init chunks
# ---------------------------------------------------------------------------


class InitChunksRequest(BaseModel):
    duration_sec: float
    # The scene window (src/producers/scenes/detail.py window_sec): chunks
    # are whole windows, so none ends inside a scene.
    scene_sec: float = Field(gt=0, le=3600)
    # The clip's scenes were found another way than lineage says (a redo):
    # new ones are found beside them. The clip shows its scenes, described,
    # until the new ones are found and described, then the new ones
    # (ADR-016, principle 11); scenes hold no human data.
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
    """The clip's chunks, made once (idempotent). With redo: a new run, found
    beside the clip's scenes, when they were found another way than lineage
    says (422 lineage_required when it doesn't say); a redo sent again,
    while its run is under way or once it's found, changes nothing."""
    run = None
    if body.redo:
        made = require_lineage(body.lineage, "scenes")
        _now_current(session, made)
        run = _redo_beside(session, asset_id, made)
    chunk_repo = VideoIndexChunkRepository(session)
    created = chunk_repo.create_chunks_for_asset(asset_id, body.duration_sec, body.scene_sec, run=run)
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


def _same_run(run: dict | None, made: dict) -> bool:
    """Whether a run's chunks find scenes as made says (its level too)."""
    keys = ("producer", "version", "settings_hash", "level")
    return run is not None and all(str(run.get(k) or "") == str(made.get(k) or "") for k in keys)


def _redo_beside(session: Session, asset_id: str, made: dict) -> dict | None:
    """A redo's run, found beside the scenes the clip shows (which stay, with
    their descriptions, images and search entries, until the new ones are
    found and described: _swap_in). Its own run under way goes on. Returns
    the run its chunks record, or None when there's nothing to find beside:
    the clip shows no scenes yet (its first run goes on) or shows them found
    this way already."""
    row = session.execute(text(
        "SELECT a.video_indexed, l.producer, l.producer_version, l.settings_hash"
        " FROM assets a LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = 'scenes'"
        " WHERE a.asset_id = :a FOR UPDATE OF a"), {"a": asset_id}).first()
    if row is None or not row.video_indexed or (row.producer, row.producer_version, row.settings_hash) == (
            made["producer"], str(made.get("version") or ""), made.get("settings_hash")):
        session.commit()
        return None
    run = session.execute(text("SELECT run FROM video_index_chunks WHERE asset_id = :a LIMIT 1"),
                          {"a": asset_id}).scalar()
    if _same_run(run, made):
        session.commit()
        return made
    # The chunks the scenes shown were found with, or another run's, go, with
    # the scenes that run staged; the scenes shown stay.
    dropped = session.execute(text("DELETE FROM video_scenes WHERE asset_id = :a AND staged IS NOT NULL"),
                              {"a": asset_id}).rowcount
    session.execute(text("DELETE FROM video_index_chunks WHERE asset_id = :a"), {"a": asset_id})
    session.commit()
    _log.info("Scenes of %s found again beside the ones shown (%d staged by another run dropped)", asset_id, dropped)
    return made


def _shows_scenes(session: Session, asset_id: str) -> bool:
    """Whether the clip's scenes are found: a run now finds new ones beside them."""
    return bool(session.execute(text("SELECT video_indexed FROM assets WHERE asset_id = :a"),
                                {"a": asset_id}).scalar())


def _described_here(request: Request) -> bool:
    """Whether the account has a model describing scenes. Without one nothing
    would describe a redo's scenes, so they're shown as soon as they're found."""
    from src.server.api.routers.producers import tenant_ai
    from src.shared.producers import PRODUCERS

    models, _ = tenant_ai(request)
    return bool(models.get(PRODUCERS["scene_vision"].job))


def _swap_in(request: Request, session: Session, asset_id: str, described: dict | None) -> bool:
    """A redo's found scenes become the ones the clip shows, in one step: the
    old ones go (and with them what search found of them), then their images
    (at keys the server derives, and not where a new scene's is). described:
    how the new ones were described (recorded for the clip), or None when
    nothing describes them. False when there's nothing to swap in (done already)."""
    session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset_id})
    new = session.execute(text("SELECT scene_id, rep_frame_ms FROM video_scenes"
                               " WHERE asset_id = :a AND staged = 'found'"), {"a": asset_id}).all()
    if not new:
        session.rollback()
        return False
    old = session.execute(text("SELECT scene_id, rep_frame_ms FROM video_scenes"
                               " WHERE asset_id = :a AND staged IS NULL"), {"a": asset_id}).all()
    session.execute(text("DELETE FROM video_scenes WHERE asset_id = :a AND staged IS NULL"), {"a": asset_id})
    session.execute(text("UPDATE video_scenes SET staged = NULL WHERE asset_id = :a AND staged = 'found'"),
                    {"a": asset_id})
    if described is not None:
        record_lineage(session, asset_id, "scene_vision", described, commit=False)
    library_id = session.execute(text("SELECT library_id FROM assets WHERE asset_id = :a"), {"a": asset_id}).scalar()
    session.commit()
    _log.info("Scenes of %s swapped in: %d new, %d old ones dropped", asset_id, len(new), len(old))
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return True
    store = get_store()
    # Only the images at the keys the server derives (a scene's rep frame),
    # and not one a new scene shares (the same moment of the same video).
    for ms in {r.rep_frame_ms for r in old} - {r.rep_frame_ms for r in new}:  # best effort: only space
        try:
            store.delete(stored.scene_rep_key(tenant_id, library_id, asset_id, ms))
        except OSError as exc:  # Unavailable too: the daily sweep takes it once it can
            _log.warning("Couldn't delete a scene image of %s: %s", asset_id, exc)
    return True


# ---------------------------------------------------------------------------
# Claim next chunk
# ---------------------------------------------------------------------------


class ChunkWorkOrder(BaseModel):
    chunk_id: str
    worker_id: str
    chunk_index: int
    start_ts: float
    end_ts: float
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
        if e.status == "claimed":  # another process has it (one stopped mid-chunk: until its lease runs out)
            raise ConflictError("chunk_under_way", f"Another chunk of this clip is under way ({e}).") from e
        raise ConflictError("chunk_failed", f"A chunk of this clip failed this run ({e}).") from e
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
    # How the scenes were found (with the level, its Detail); recorded when
    # the clip's last chunk completes, and the discard pass merges at that level.
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

    # A clip that shows scenes already is a redo's: its scenes are found beside them.
    staged = "finding" if _shows_scenes(session, chunk.asset_id) else None
    ok = chunk_repo.complete_chunk(
        chunk_id=chunk_id,
        worker_id=body.worker_id,
        scenes=[s.model_dump() for s in body.scenes],
        staged=staged,
    )
    if not ok:
        session.rollback()
        raise HTTPException(status_code=409, detail="Chunk not claimable by this worker")

    asset_id = chunk.asset_id
    all_done = chunk_repo.all_chunks_complete(asset_id)
    show_now = False

    if all_done and asset_id:
        from src.producers.scenes.detail import detail, name_of
        from src.server.repository import lineage

        # The discard pass, with the last chunk, before the clip counts as
        # found: scene descriptions only ever see the scenes kept.
        found, kept = chunk_repo.merge_alike(asset_id, detail(name_of(made.get("level"))).same_within, staged)
        _log.info("Scenes of %s: %d found, %d kept (detail %s)", asset_id, found, kept, name_of(made.get("level")))
        # How they were found lands with them: a redo in between sees both or neither.
        record_lineage(session, asset_id, "scenes", made, commit=False)
        if staged:
            # Found: described next, while the clip still shows its old ones,
            # then swapped in by the last description (update_scene_vision).
            session.execute(text("UPDATE video_scenes SET staged = 'found' WHERE asset_id = :a AND staged = 'finding'"),
                            {"a": asset_id})
            lineage.start_over(session, [asset_id], "scene_vision")
            show_now = not _described_here(request)
        else:
            session.execute(text("UPDATE assets SET video_indexed = true, updated_at = now() WHERE asset_id = :a"),
                            {"a": asset_id})
    session.commit()
    if show_now:
        _swap_in(request, session, asset_id, None)
    if all_done and asset_id and not staged:
        asset_repo = AssetRepository(session)
        # Inline search sync (best-effort)
        asset_obj = asset_repo.get_by_id(asset_id)
        if asset_obj:
            meta_obj = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
            if meta_obj:
                from src.server.search.sync import try_sync_asset
                try_sync_asset(session, asset_obj, meta_obj)

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
    describing: bool = False,
) -> ScenesResponse:
    """The scenes the video shows, ordered by start_ms. describing (the
    scene describer's): the ones to describe next, a redo's found scenes
    while it has them, else the ones shown."""
    scene_repo = VideoSceneRepository(session)
    scenes = scene_repo.get_scenes_for_asset(asset_id, describing=describing)
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
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> SceneVisionUpdateResponse:
    """Update vision results on a scene after describing its rep frame; it
    says how (422 lineage_required, before anything is saved). The last of a
    redo's found scenes described swaps them in for the ones the clip shows."""
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
    # settings and half with new isn't current, and its redo isn't done. A
    # redo's found scenes, all described, are swapped in with that record.
    row = session.execute(text("SELECT asset_id, staged FROM video_scenes WHERE scene_id = :s"),
                          {"s": scene_id}).first()
    if row is not None and row.staged != "finding":
        others = session.execute(text(
            "SELECT count(*) FROM video_scenes WHERE asset_id = :a"
            " AND staged IS NOT DISTINCT FROM CAST(:st AS text)"
            " AND (description IS NULL OR lineage IS DISTINCT FROM CAST(:l AS jsonb))"
        ), {"a": row.asset_id, "st": row.staged, "l": json.dumps(_scene_lineage(made))}).scalar()
        if not others and row.staged == "found":
            _swap_in(request, session, row.asset_id, made)
        elif not others:
            record_lineage(session, row.asset_id, "scene_vision", made)
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
# Retired: a scene's search sync
# ---------------------------------------------------------------------------


@router.post("/scenes/{scene_id}/sync", dependencies=[Depends(require_editor)])
def sync_scene(scene_id: str) -> None:
    """Retired (410 gone): a scene's description and tags are searchable as
    soon as they're saved (ADR-019: its words are computed with its row)."""
    raise GoneError(
        "POST /v1/video/scenes/{scene_id}/sync is retired: a scene's description and tags are searchable "
        "as soon as they're saved.",
        ["PATCH /v1/video/scenes/{scene_id}"],
    )


# ---------------------------------------------------------------------------
# A video's faces (src/producers/video_faces)
# ---------------------------------------------------------------------------

# A video's people a request, and the times one was seen at (a frame every
# 3 s of a day-long video is 28,800).
MAX_VIDEO_FACES = 500
MAX_SEEN = 50_000
MAX_CROP_BYTES = 256 * 1024


class VideoFaceIn(BaseModel):
    """One person seen in a video: their face as shown (its box in the frame
    at frame_ms, {x, y, w, h} as fractions, or {x1, y1, x2, y2}), and every
    time (ms) they were seen at."""

    bounding_box: dict[str, float]
    detection_confidence: float
    embedding: list[float] | None = None
    frame_ms: int = Field(ge=0)
    seen_ms: list[int] = Field(min_length=1, max_length=MAX_SEEN)


class VideoFacesResponse(BaseModel):
    face_count: int
    face_ids: list[str]


def _webp(data: bytes) -> bool:
    """A WebP the server reads (src/server/images.py): only the WebP reader looks at it."""
    from src.server.images import opened

    try:
        with opened(data, formats=("WEBP",)) as img:
            img.verify()
    except Exception:  # noqa: BLE001 — not a WebP the server reads
        return False
    return True


@router.post("/{asset_id}/faces", response_model=VideoFacesResponse, dependencies=[Depends(require_editor)])
def submit_video_faces(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    faces: Annotated[str, Form()],
    lineage: Annotated[str, Form()] = "",
    detection_model: Annotated[str, Form(max_length=100)] = "insightface",
    detection_model_version: Annotated[str, Form(max_length=100)] = "buffalo_l",
    embedding_model: Annotated[str, Form(max_length=100)] = "buffalo_l",
    source_hash: Annotated[str, Form(max_length=300)] = "",
    crops: Annotated[list[UploadFile] | None, File()] = None,
) -> VideoFacesResponse:
    """A video's faces, one a person seen (multipart): faces, a JSON list of
    VideoFaceIn; crops, one 128 x 128 WebP a face in the same order, cut from
    its frame (the server has no frames); lineage, how they were found (JSON).
    Saved as a photo's are (FaceRepository.submit_faces): faces people named,
    or said aren't someone, are kept; the rest are found again."""
    from src.server.api.routers.assets import _normalize_bounding_box
    from src.server.repository import lineage as lineage_repo

    made = require_lineage(lineage, "video_faces")  # before anything is saved
    if source_hash and (why := why_not(source_hash)) is not None:
        raise ApiError(400, "content_hash_not_valid", f"source_hash: {why}", {"source_hash": source_hash[:100]})
    try:
        items = TypeAdapter(list[VideoFaceIn]).validate_json(faces)
    except ValidationError as e:
        raise ApiError(400, "faces_not_valid", "faces is a JSON list of faces, each with bounding_box,"
                       " detection_confidence, embedding, frame_ms and seen_ms.",
                       {"errors": e.errors(include_url=False, include_context=False)[:5]}) from None
    if len(items) > MAX_VIDEO_FACES:
        raise ApiError(400, "too_many_faces", f"At most {MAX_VIDEO_FACES} faces a video.", {"sent": len(items)})
    crops = crops or []
    if len(crops) != len(items):
        raise ApiError(400, "crops_dont_match", "One crop a face, in the order of the faces.",
                       {"faces": len(items), "crops": len(crops)})
    images: list[bytes] = []
    for i, crop in enumerate(crops):
        data = crop.file.read(MAX_CROP_BYTES + 1)
        if len(data) > MAX_CROP_BYTES or not _webp(data):
            raise ApiError(400, "crop_not_valid", f"A crop is a WebP image, up to {MAX_CROP_BYTES // 1024} KB.",
                           {"index": i})
        images.append(data)

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise ApiError(400, "not_a_video", "Faces with times are a video's; a photo's go to /v1/assets/batch-faces.",
                       {"media_type": asset.media_type})

    dropped = AfterCommit()
    face_ids = FaceRepository(session).submit_faces(
        asset_id=asset_id,
        detection_model=detection_model,
        detection_model_version=detection_model_version,
        faces=[{"bounding_box": _normalize_bounding_box(f.bounding_box),
                "detection_confidence": f.detection_confidence,
                "embedding": [float(x) for x in f.embedding] if f.embedding else None,
                "frame_ms": f.frame_ms,
                "seen_ms": sorted({*f.seen_ms, f.frame_ms})} for f in items],
        embedding_model=embedding_model,
        after=dropped,
    )
    lineage_repo.record(session, asset_id, "video_faces", with_source(made, source_hash or None),
                        outcome="ok" if items else "empty")  # commits
    dropped.run()  # the crops of faces not found again

    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id:
        store = get_store()
        for face_id, data in zip(face_ids, images, strict=True):
            key = stored.face_crop_key(tenant_id, asset.library_id, face_id)
            store.write(key, data)
            session.execute(text("UPDATE faces SET crop_key = :key WHERE face_id = :fid"), {"key": key, "fid": face_id})
    session.commit()
    LibraryRepository(session).bump_revision(asset.library_id)
    session.refresh(asset)
    return VideoFacesResponse(face_count=asset.face_count or 0, face_ids=face_ids)
