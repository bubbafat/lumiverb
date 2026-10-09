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

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from PIL import Image
from sqlmodel import Session

from src.shared.io_utils import normalize_rel_path
from src.server.api.dependencies import get_tenant_session
from src.shared import asset_status
from src.shared.path_filter import PathFilter, is_path_included_merged
from src.server.repository.tenant import (
    AssetEmbeddingRepository,
    AssetMetadataRepository,
    AssetRepository,
    LibraryRepository,
    PathFilterRepository,
)
from src.server.storage.local import LocalStorage, get_storage

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
    try:
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
    finally:
        img.close()


def _generate_thumbnail(proxy_bytes: bytes) -> bytes:
    """Generate a thumbnail from proxy bytes. Returns WebP bytes."""
    img = Image.open(io.BytesIO(proxy_bytes))
    try:
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
    finally:
        img.close()


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


def _per_kind(raw: str | None) -> dict:
    """The lineage form field: a JSON object by artifact kind, or {}."""
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _required_lineage(raw: str | None, vision_data: dict | None, embeddings_data: list[dict] | None,
                      facet_data: dict | None = None) -> dict[str, dict]:
    """The lineage of each kind the ingest stores, which it must give
    (422 lineage_required, before anything is saved): the proxy always, a
    description with a model, CLIP's vectors, a probe."""
    from src.server.api.routers.producers import require_lineage
    from src.shared.producers import CLIP_MODEL_ID

    by_kind = _per_kind(raw)
    kinds = ["proxy"]
    if vision_data is not None and vision_data.get("model_id"):
        kinds.append("vision")
    if embeddings_data and any(e.get("model_id") == CLIP_MODEL_ID for e in embeddings_data):
        kinds.append("clip")
    if facet_data is not None:
        kinds.append("probe")
    return {kind: require_lineage(by_kind.get(kind), kind) for kind in kinds}


def _forget_scenes(session: Session, asset_id: str) -> None:
    """A clip's scenes, their chunks and descriptions are gone: found again
    from the new content. (Their search documents go once this commits;
    rep-frame files are left for the orphan cleanup.)"""
    from sqlalchemy import text as sa_text

    session.execute(sa_text("DELETE FROM video_scenes WHERE asset_id = :a"), {"a": asset_id})
    session.execute(sa_text("DELETE FROM video_index_chunks WHERE asset_id = :a"), {"a": asset_id})
    session.execute(sa_text("DELETE FROM artifact_lineage WHERE asset_id = :a AND artifact IN ('scenes', 'scene_vision')"),
                    {"a": asset_id})


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
    lineage_by_kind: dict | None = None,
) -> IngestResponse:
    """Core ingest logic shared by both endpoints. Records how the proxy (and
    any vision or embeddings sent along) were made: lineage_by_kind maps an
    artifact kind to its lineage, checked by _required_lineage."""
    from src.server.repository.lineage import record as record_lineage
    from src.shared.producers import CLIP_MODEL_ID

    lineage_by_kind = lineage_by_kind or {}
    storage: LocalStorage = get_storage()
    asset_repo = AssetRepository(session)

    # --- Normalize proxy to WebP ---
    try:
        proxy_bytes, proxy_w, proxy_h = _normalize_proxy(raw_proxy)
    except Exception:
        logger.exception("Failed to process proxy image for asset %s", asset_id)
        raise HTTPException(status_code=400, detail="Failed to process proxy image")

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

    # The file was replaced: its analysis proxy and scenes show the old
    # content, so they're made again. Everything else made from it is handed
    # out again by the reconciler (its lineage names the old file). Both
    # ingest endpoints come through here, before the new SHA-256 is stored.
    scenes_forgotten = False
    new_sha = (exif_data or {}).get("sha256")
    current = asset_repo.get_by_id(asset_id) if new_sha else None
    if current is not None and current.sha256 and new_sha != current.sha256:
        current.analysis_proxy_key = None
        current.analysis_proxy_sha256 = None
        current.analysis_proxy_generated_at = None
        if current.video_indexed or current.media_type == "video":
            _forget_scenes(session, asset_id)
            current.video_indexed = False
            scenes_forgotten = True
        session.add(current)

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
            exposure_time_us=exif_data.get("exposure_time_us"),
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
            # Inline search sync (best-effort)
            asset_obj = asset_repo.get_by_id(asset_id)
            meta_obj = meta_repo.get_latest(asset_id=asset_id)
            if asset_obj and meta_obj:
                from src.server.search.sync import try_sync_asset
                try_sync_asset(session, asset_obj, meta_obj, tenant_id=tenant_id)
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

    record_lineage(session, asset_id, "proxy", lineage_by_kind.get("proxy"), commit=False)
    # Only what was stored: a description with no model is dropped above,
    # and only the CLIP producer's vectors are its artifact.
    if vision_data is not None and vision_data.get("model_id"):
        record_lineage(session, asset_id, "vision", lineage_by_kind.get("vision"), commit=False)
    if embeddings_data and any(e.get("model_id") == CLIP_MODEL_ID for e in embeddings_data):
        record_lineage(session, asset_id, "clip", lineage_by_kind.get("clip"), commit=False)

    # Commit all changes atomically. The tenant session does NOT auto-commit
    # (SQLModel's `with Session` rolls back on exit), so every write path
    # must commit explicitly. The update-existing branch in create_and_ingest
    # already commits; this covers the new-asset branch and the standalone
    # /v1/assets/{id}/ingest endpoint.
    session.commit()
    if scenes_forgotten:
        from src.server.search.quickwit_client import QuickwitClient

        QuickwitClient().delete_scene_index_documents_by_asset_ids(tenant_id, [asset_id])

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


def _parse_video_facet(raw: str | None, media_type: str) -> dict | None:
    """Validate the optional video_facet form field (a probe result, see VideoFacetModel)."""
    if raw is None:
        return None
    from pydantic import ValidationError

    from src.server.api.routers.assets import VideoFacetModel

    if media_type != "video":
        raise HTTPException(status_code=400, detail="video_facet is only for video assets")
    data = _parse_optional_json(raw, "video_facet")
    try:
        return VideoFacetModel(**(data or {})).model_dump()
    except ValidationError as exc:
        # A probe value we can't accept never blocks ingest: keep the video,
        # leave it unprobed (enrich --job-type probe can retry).
        logger.warning("Ignoring invalid video_facet: %s", exc.errors()[:3])
        return None


@router.post("/v1/ingest", response_model=IngestResponse)
async def create_and_ingest(
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    proxy: UploadFile = File(...),
    library_id: str = Form(...),
    rel_path: str = Form(...),
    file_size: int = Form(...),
    file_mtime: str | None = Form(default=None),
    media_type: str = Form(default="image"),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    exif: str | None = Form(default=None),
    vision: str | None = Form(default=None),
    embeddings: str | None = Form(default=None),
    video_facet: str | None = Form(default=None),
    # JSON {"proxy": {...}, "probe": {...}}: how each artifact sent was made (see LineageIn)
    lineage: str | None = Form(default=None),
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
      - media_type: "image" or "video" (default "image")
      - width/height: original source dimensions
      - exif, vision, embeddings: JSON strings (same as /v1/assets/{id}/ingest)
    """
    if media_type not in ("image", "video"):
        raise HTTPException(status_code=400, detail="media_type must be 'image' or 'video'")
    if not rel_path or ".." in rel_path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid rel_path")
    if width is not None and (width < 1 or width > 100_000):
        raise HTTPException(status_code=400, detail="width out of range")
    if height is not None and (height < 1 or height > 100_000):
        raise HTTPException(status_code=400, detail="height out of range")
    if proxy.content_type and not proxy.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Proxy must be an image file")

    raw_proxy = await proxy.read()
    if len(raw_proxy) > MAX_PROXY_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Proxy upload too large (10 MB max)")
    if not raw_proxy:
        raise HTTPException(status_code=400, detail="Proxy file is empty")

    tenant_id: str = request.state.tenant_id
    rel_path = normalize_rel_path(rel_path)

    # Validate library
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    # A trashed library takes no new clips and doesn't get its clips back
    # from a scan already under way (or a client still holding its id).
    if library.status == "trashed":
        raise HTTPException(status_code=409, detail="Library is in the trash")

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
    facet_data = _parse_video_facet(video_facet, media_type)
    made = _required_lineage(lineage, vision_data, embeddings_data, facet_data)

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
    reappeared = None  # set when a file the scanner marked missing is back
    created = False

    # A different file at the same path is a new clip, and the old one goes
    # missing (Robert, Oct 9): archived where it was, with everything it had,
    # back if its content is. Only when both contents are known.
    sha = (exif_data or {}).get("sha256")
    if existing is not None and sha and existing.sha256 and existing.sha256 != sha:
        if existing.deleted_at is None:
            from src.server.api.routers.assets import _out_of_search

            asset_repo.trash_many([existing.asset_id], reason="missing")
            _out_of_search(background, request, [existing.asset_id])
        existing = None

    # An archived asset back at its own path: lock it before restoring. A copy
    # of the file ingested at the same time may have claimed it by content;
    # then this path is new to the library.
    if existing is not None and existing.deleted_at is not None and not asset_repo.lock_for_restore(existing, rel_path):
        existing = None
    if existing is None:
        if asset_repo.is_ignored(library_id, rel_path):
            raise HTTPException(
                status_code=409,
                detail=f"File was removed from the library (trash emptied): {rel_path}",
            )
        # The archive model: a missing file's content may have turned up here
        # (moved, or renamed while the scan wasn't looking). Restore that asset
        # at its new path, with everything it had, instead of starting over;
        # unless the account doesn't follow moves (the path is the identity).
        from src.server.tenant_settings import get_follow_moves

        archived = (asset_repo.find_missing_by_sha(library_id, (exif_data or {}).get("sha256"))
                    if get_follow_moves(session) else None)
        if archived is not None:
            archived.rel_path = rel_path
            existing = archived
    if existing is None:
        asset = asset_repo.create_asset(
            library_id=library_id,
            rel_path=rel_path,
            file_size=file_size,
            file_mtime=file_mtime_dt,
            media_type=media_type,
        )
        asset_id = asset.asset_id
        created = True
    else:
        # A person's trash or archive is human data: a rescan of a file still
        # on disk never undoes it. Scanners skip these via
        # GET /v1/libraries/{id}/ignored-paths.
        if existing.deleted_at is not None and existing.deleted_reason in ("user", "archived"):
            where = "in the trash" if existing.deleted_reason == "user" else "archived"
            raise HTTPException(status_code=409, detail=f"Asset is {where}: {rel_path}")
        asset_id = existing.asset_id
        # Update file metadata if changed
        existing.file_size = file_size
        if file_mtime_dt is not None:
            existing.file_mtime = file_mtime_dt
        existing.media_type = media_type
        # A file the scanner marked missing has reappeared: restore it, and
        # put it back in search (trashing deleted its search documents).
        # Without this, the asset stays invisible (active_assets filters
        # deleted_at) and the scanner re-discovers it every cycle.
        if existing.deleted_at is not None:
            AssetRepository(session).clear_trash(existing)
            reappeared = existing
        session.add(existing)

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
        lineage_by_kind=made,
    )
    if facet_data is not None:
        asset_repo.upsert_video_facet(asset_id, facet_data)
        from src.server.repository.lineage import record as record_lineage

        record_lineage(session, asset_id, "probe", made["probe"])
    if reappeared is not None and reappeared.transcript_srt:
        from src.server.search.sync import index_transcript_segments

        index_transcript_segments(tenant_id, reappeared)
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
    lineage: str | None = Form(default=None),  # JSON by artifact kind, as for POST /v1/ingest
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
    made = _required_lineage(lineage, vision_data, embeddings_data)

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
        lineage_by_kind=made,
    )
