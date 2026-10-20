"""Uploads in chunks, for a clip's video preview or analysis proxy (ADR-018
decision 9). The rules are in src/server/uploads.py.

POST /v1/assets/{id}/uploads starts one (or picks up the open one of the
same file); PUT its chunk_url with each chunk at its offset; POST
/v1/uploads/{id}/finish keeps it, checked and rewritten as a single upload
would be (POST /v1/assets/{id}/artifacts/{type}). Every call is safe to
send again: a start is the same upload, a chunk adds only what's new, a
finish gets the answer the first got. Editors and admins; another
account's upload is a 404.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, BeforeValidator, Field
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from src.producers.names import ANALYSIS_PROXY, VIDEO_PREVIEW, made_by_file, now_called
from src.server import uploaded_video, uploads
from src.server.api.dependencies import get_tenant_session, require_editor
from src.server.api.devices import may_call
from src.server.api.errors import ApiError, ConflictError
from src.server.repository.assets import AssetRepository
from src.shared.utils import utcnow

router = APIRouter(tags=["uploads"], dependencies=[Depends(require_editor)])


def _file(value: Any) -> Any:
    """A file by its name; by its old kind, a 422 naming the name (refused since ADR-024's part 5)."""
    name = now_called(value, "file") if isinstance(value, str) else None
    if name:
        raise ValueError(f"{value} is called {name} now: send its name")
    return value


class StartUpload(BaseModel):
    # By the file's name (lumiverb.video.preview, lumiverb.video.analysis_proxy: ADR-024).
    artifact: Annotated[Literal[VIDEO_PREVIEW, ANALYSIS_PROXY], BeforeValidator(_file)]
    size: int = Field(gt=0, le=64 * 1024**3)
    # The whole file's SHA-256, 64 lowercase hex digits: checked at the finish.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage: Any  # how it was made (LineageIn), as a single upload's form field says it


class Upload(BaseModel):
    upload_id: str
    artifact: str
    size: int
    # Bytes the server has: send from here.
    received: int
    # Where each chunk goes (PUT, with its offset), and the size the server suggests for one.
    chunk_url: str
    chunk_size: int
    max_chunk: int
    expires_at: datetime
    finished: bool = False


class Received(BaseModel):
    received: int


class Kept(BaseModel):
    key: str
    sha256: str
    duration_sec: float | None = None


def _shown(upload: dict) -> Upload:
    return Upload(upload_id=upload["upload_id"], artifact=upload["artifact"], size=upload["size"],
                  received=upload["received"], chunk_url=f"/v1/uploads/{upload['upload_id']}",
                  chunk_size=uploads.CHUNK_SIZE, max_chunk=uploads.MAX_CHUNK, expires_at=upload["expires_at"],
                  finished=upload["finished_at"] is not None if "finished_at" in upload else False)


def _held(session: Session, upload_id: str, *, lock: bool = True) -> dict:
    """The upload, locked; 404 when there's none (or it's another account's: not in this database)."""
    try:
        upload = uploads.get(session, upload_id, lock=lock)
    except ValueError:
        upload = None
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return upload


@router.post("/v1/assets/{asset_id}/uploads", response_model=Upload)
@may_call("asset")
def start_upload(asset_id: str, body: StartUpload, request: Request,
                 session: Annotated[Session, Depends(get_tenant_session)]) -> Upload:
    """Start an upload of the clip's video preview or analysis proxy, or
    pick up the open one of the same file (same size and SHA-256): its
    received says where to go on from. 404 unknown clip; 400 not a video;
    422 lineage_required."""
    from src.server.api.routers.producers import require_lineage

    made = require_lineage(body.lineage, made_by_file(body.artifact))  # the file's kind; lineage's by name
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Video previews and analysis proxies are for videos only")
    upload = uploads.start(session, request.state.tenant_id, asset_id=asset_id, artifact=body.artifact,
                           size=body.size, sha256=body.sha256, lineage=made, now=utcnow())
    session.commit()
    return _shown(upload)


@router.get("/v1/uploads/{upload_id}", response_model=Upload)
@may_call("upload")
def upload_status(upload_id: str, session: Annotated[Session, Depends(get_tenant_session)]) -> Upload:
    """Where an upload is: its received, and whether it's finished."""
    return _shown(_held(session, upload_id, lock=False))


@router.put("/v1/uploads/{upload_id}", response_model=Received)
@may_call("upload")
async def put_chunk(upload_id: str, request: Request, session: Annotated[Session, Depends(get_tenant_session)],
                    offset: int = Query(ge=0)) -> Received:
    """A chunk, the request's body, beginning at offset in the file: what's
    new in it is added (a chunk sent again adds nothing). Returns received.
    409 upload_gap (details.received) for one beginning past what the
    server has; 413 past max_chunk or the upload's size; 409
    upload_finished once it's kept."""
    tenant_id = request.state.tenant_id
    if not upload_id.startswith("upl_") or "/" in upload_id:
        raise HTTPException(status_code=404, detail="Upload not found")
    chunk = uploads.chunk_file(tenant_id, upload_id)
    try:
        total = 0
        with open(chunk, "wb") as f:
            async for data in request.stream():
                total += len(data)
                if total > uploads.MAX_CHUNK:
                    raise HTTPException(status_code=413, detail=f"A chunk is at most {uploads.MAX_CHUNK} bytes")
                f.write(data)
        upload = _held(session, upload_id)
        if upload["finished_at"] is not None:
            raise ConflictError("upload_finished", "This upload is kept already.")
        try:
            received = await run_in_threadpool(uploads.add, session, tenant_id, upload, offset, chunk, utcnow())
        except uploads.GapError as gap:
            raise ConflictError("upload_gap", str(gap), {"received": gap.received}) from None
        except uploads.TooBigError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from None
        session.commit()
        return Received(received=received)
    finally:
        chunk.unlink(missing_ok=True)


@router.post("/v1/uploads/{upload_id}/finish", response_model=Kept)
@may_call("upload")
async def finish_upload(upload_id: str, request: Request,
                        session: Annotated[Session, Depends(get_tenant_session)]) -> Kept:
    """Keep the upload: every byte there and the whole file's SHA-256 as
    said, then the video checked and rewritten (422 video_not_playable) and
    kept as a single upload keeps it. Sent again, the answer the first got.
    409 upload_incomplete (details.received) before every byte is there;
    422 upload_damaged when the bytes aren't the file said (the upload is
    dropped: start again)."""
    from src.server.api.routers.artifacts import artifact_key, record_kept
    from src.server.storage.store import get_store

    tenant_id = request.state.tenant_id
    upload = _held(session, upload_id)
    if upload["finished_at"] is not None:
        session.rollback()
        return Kept(**upload["answer"])
    if upload["received"] != upload["size"]:
        raise ConflictError("upload_incomplete", f"The upload has {upload['received']} of {upload['size']} bytes.",
                            {"received": upload["received"], "size": upload["size"]})
    if await run_in_threadpool(uploads.whole_sha256, tenant_id, upload_id) != upload["sha256"]:
        uploads.drop(session, tenant_id, upload_id)
        session.commit()
        raise ApiError(422, "upload_damaged", "The bytes received aren't the file the upload said: start again.")
    asset = AssetRepository(session).get_by_id(upload["asset_id"])
    if asset is None or asset.deleted_at is not None:
        uploads.drop(session, tenant_id, upload_id)
        session.commit()
        raise HTTPException(status_code=404, detail="Asset not found")
    part = uploads.part(tenant_id, upload_id)
    try:
        kept = await run_in_threadpool(uploaded_video.keep, part)
    except uploaded_video.NotPlayableError as exc:
        uploads.drop(session, tenant_id, upload_id)
        session.commit()
        raise ApiError(422, "video_not_playable",
                       f"{upload['artifact']} isn't a video every browser plays: {exc}. Send H.264 (8-bit 4:2:0,"
                       " at most High profile, limited range) with AAC-LC audio, at most stereo, in an MP4.",
                       {"problems": exc.problems}) from None
    key = artifact_key(tenant_id, asset, upload["artifact"])
    await run_in_threadpool(_into_the_store, get_store(), key, part)
    record_kept(session, asset.asset_id, upload["artifact"], key, kept.sha256, upload["lineage"])
    answer = {"key": key, "sha256": kept.sha256, "duration_sec": kept.duration_sec}
    uploads.finished(session, upload_id, answer, utcnow())
    session.commit()
    part.unlink(missing_ok=True)
    return Kept(**answer)


def _into_the_store(store, key: str, path) -> None:
    """The kept file into the Store, seen only once whole."""
    with store.writing(key, suffix=".mp4") as partial, open(path, "rb") as src, open(partial, "wb") as dest:
        shutil.copyfileobj(src, dest, 1024 * 1024)


@router.delete("/v1/uploads/{upload_id}", status_code=204)
@may_call("upload")
def drop_upload(upload_id: str, request: Request, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Drop an upload and its bytes. 404 when there's none."""
    _held(session, upload_id)
    uploads.drop(session, request.state.tenant_id, upload_id)
    session.commit()

