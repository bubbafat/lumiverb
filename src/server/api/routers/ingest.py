"""Atomic ingest endpoints: create + populate assets in one request.

POST /v1/ingest — create asset record AND ingest proxy + metadata atomically.
POST /v1/assets/{asset_id}/ingest — ingest into an existing asset record.

The server normalizes the proxy (WebP, 2048px max), generates a thumbnail
(WebP, 512px), and stores all provided metadata atomically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from src.server import preview_image
from src.server.api.content_hashes import why_not
from src.server.api.dependencies import checked_rel_path, get_tenant_session, require_editor
from src.shared import asset_status
from src.shared.content_hash import sampled, whole_file
from src.shared.file_extensions import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
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
THUMBNAIL_LONG_EDGE = preview_image.THUMBNAIL_LONG_EDGE
WEBP_QUALITY = 80
MAX_PROXY_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB raw upload limit


def _normalize_proxy(data: bytes) -> tuple[bytes, int, int]:
    """The client's proxy as stored: WebP, at most PROXY_MAX_LONG_EDGE, with
    nothing of the original's metadata (preview_image.py), a WebP too.
    Returns (webp_bytes, width, height), the proxy's own dimensions."""
    from src.server.preview_image import reencoded

    return reencoded(data, fmt="WEBP", quality=WEBP_QUALITY, max_long_edge=PROXY_MAX_LONG_EDGE)


def _generate_thumbnail(proxy_bytes: bytes) -> bytes:
    """Generate a thumbnail from proxy bytes. Returns WebP bytes (preview_image.thumbnail)."""
    return preview_image.thumbnail(proxy_bytes)


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
        data = json.loads(field)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"{field_name} must be valid JSON")
    if data is not None and not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a JSON object")
    return data


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
                      facet_data: dict | None = None, exif_data: dict | None = None) -> dict[str, dict]:
    """The lineage of each kind the ingest stores, which it must give
    (422 lineage_required, before anything is saved): the proxy always, a
    description with a model, CLIP's vectors, a probe. The capture facts
    sent with the EXIF count as made only when it says how (the Python
    scan does); otherwise the capture producer reads them again."""
    from src.server.api.routers.producers import require_lineage
    from src.shared.producers import CLIP_MODEL_ID

    by_kind = _per_kind(raw)
    kinds = ["proxy"]
    if exif_data is not None and by_kind.get("capture") is not None:
        kinds.append("capture")
    if vision_data is not None and vision_data.get("model_id"):
        kinds.append("vision")
    if embeddings_data and any(e.get("model_id") == CLIP_MODEL_ID for e in embeddings_data):
        kinds.append("clip")
    if facet_data is not None:
        kinds.append("probe")
    return {kind: require_lineage(by_kind.get(kind), kind) for kind in kinds}


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
    proxy_path = storage.write_path(proxy_key)
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    proxy_path.write_bytes(proxy_bytes)
    proxy_sha256 = hashlib.sha256(proxy_bytes).hexdigest()

    # --- Write thumbnail to storage ---
    thumb_key = storage.thumbnail_key(tenant_id, library_id, asset_id, rel_path)
    thumb_path = storage.write_path(thumb_key)
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(thumb_bytes)
    thumb_sha256 = hashlib.sha256(thumb_bytes).hexdigest()

    # --- Update DB: proxy + thumbnail ---
    source_w = width if width is not None else proxy_w
    source_h = height if height is not None else proxy_h
    asset_repo.set_proxy_artifact(asset_id, proxy_key, proxy_sha256, source_w, source_h)
    asset_repo.set_thumbnail_artifact(asset_id, thumb_key, thumb_sha256)

    final_status = asset_status.PROXY_READY

    # --- Store EXIF if provided ---
    if exif_data is not None:
        asset_repo.update_exif(
            asset_id=asset_id,
            content_hash=exif_data.get("content_hash"),
            exif=exif_data.get("exif", {}),
            camera_make=exif_data.get("camera_make"),
            camera_model=exif_data.get("camera_model"),
            taken_at=exif_data.get("taken_at"),
            gps_lat=exif_data.get("gps_lat"),
            gps_lon=exif_data.get("gps_lon"),
            gps_accuracy_m=exif_data.get("gps_accuracy_m"),
            taken_at_offset_min=exif_data.get("taken_at_offset_min"),
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
                try_sync_asset(session, asset_obj, meta_obj)
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
    if exif_data is not None:
        if "capture" in lineage_by_kind:
            record_lineage(session, asset_id, "capture", lineage_by_kind["capture"], commit=False)
        else:
            # EXIF from a scanner that doesn't say how it read the capture facts
            # (the macOS app): the capture producer reads them again.
            from src.server.repository.lineage import forget

            forget(session, [asset_id], "capture", commit=False)
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


def _exif_hashes(exif_data: dict | None) -> tuple[str | None, str | None]:
    """The file's content hash an upload's exif carries (content_hash), and
    the whole file's beside it (previous_hash): each one the server takes
    (src/server/api/content_hashes.py), else 400. Neither is required."""
    hashes = []
    for name in ("content_hash", "previous_hash"):
        value = (exif_data or {}).get(name)
        if value is not None and (why := why_not(value)) is not None:
            raise HTTPException(status_code=400, detail=f"exif {name}: {why}")
        hashes.append(value)
    return hashes[0], hashes[1]


def _swap_whole_file_hash(session: Session, library_id: str, file_size: int, content_hash: str | None,
                          previous: str | None) -> bool:
    """The file's whole "sha256" sent beside its fp1 (exif previous_hash: its
    scan's plan asked, as a clip or file deleted for good of its size has a
    hash made before fp1): those hashes are swapped for the fp1 first, so the
    file is compared with what was (src/server/scans.py rekey). Whether any
    was sent."""
    if previous is None:
        return False
    if not (whole_file(previous, file_size) and sampled(content_hash)):
        raise HTTPException(status_code=400,
                            detail="exif previous_hash is the whole file's sha256: hash beside its fp1: hash")
    from src.server.scans import rekey

    rekey(session, library_id, [(previous, content_hash, file_size)])
    return True


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


@router.post("/v1/ingest", response_model=IngestResponse, dependencies=[Depends(require_editor)])
async def create_and_ingest(
    request: Request,
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
    rel_path = checked_rel_path(rel_path)
    if posixpath.splitext(rel_path)[1].lower() not in (VIDEO_EXTENSIONS if media_type == "video" else IMAGE_EXTENSIONS):
        raise HTTPException(status_code=400, detail=f"Unsupported {media_type} file type")
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
    content_hash, previous = _exif_hashes(exif_data)
    vision_data = _parse_optional_json(vision, "vision")
    embeddings_data = _parse_optional_json_list(embeddings, "embeddings")
    facet_data = _parse_video_facet(video_facet, media_type)
    made = _required_lineage(lineage, vision_data, embeddings_data, facet_data, exif_data)

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
    created = False

    # A different file at the same path is a new clip, and the old one goes
    # missing (Robert, Oct 9): archived where it was, with everything it had,
    # back if its content is. Only when both contents are known. It goes
    # whatever becomes of this file: the file at its path isn't its any more.
    from src.server.tenant_settings import get_follow_moves

    if _swap_whole_file_hash(session, library_id, file_size, content_hash, previous):
        session.expire_all()  # the clips read before have their hashes as they are now
    if existing is not None and content_hash and existing.content_hash and existing.content_hash != content_hash:
        if existing.deleted_at is None:
            try:  # a proxy that can't be read is a bad request, which changes nothing
                _normalize_proxy(raw_proxy)
            except Exception:
                raise HTTPException(status_code=400, detail="Failed to process proxy image") from None
            asset_repo.trash_many([existing.asset_id], reason="missing")  # commits
            if get_follow_moves(session):
                # Its content may already be at another path (copied, then
                # overwritten here): it moves there, with everything it had.
                from src.server.api.routers.trash import hand_over_to_copies

                hand_over_to_copies(session, request, [existing.asset_id])
        # This content's own clip at this path, if it had one: back where it was.
        existing = asset_repo.find_at(library_id, rel_path, content_hash)

    # An archived asset back at its own path: lock it before restoring. A copy
    # of the file ingested at the same time may have claimed it by content;
    # then this path is new to the library.
    if existing is not None and existing.deleted_at is not None and not asset_repo.lock_for_restore(existing, rel_path):
        existing = None
    if existing is None:
        if asset_repo.is_ignored(library_id, rel_path, content_hash):
            raise HTTPException(
                status_code=409,
                detail=f"File was removed from the library (trash emptied): {rel_path}",
            )
        # The archive model: a missing file's content may have turned up here
        # (moved, or renamed while the scan wasn't looking). Restore that asset
        # at its new path, with everything it had, instead of starting over;
        # unless the account doesn't follow moves (the path is the identity).
        archived = asset_repo.find_missing_by_hash(library_id, content_hash) if get_follow_moves(session) else None
        if archived is not None:
            archived.rel_path = rel_path
            existing = archived
    # A person's trash or archive is human data: a rescan of a file still
    # on disk never undoes it. A scan's plan skips these (src/server/scans.py
    # REMOVED); this refuses one sent anyway.
    if existing is not None and existing.deleted_at is not None and existing.deleted_reason in ("user", "archived"):
        where = "in the trash" if existing.deleted_reason == "user" else "archived"
        raise HTTPException(status_code=409, detail=f"Asset is {where}: {rel_path}")

    if existing is None:
        from sqlalchemy.exc import IntegrityError

        try:
            asset = asset_repo.create_asset(
                library_id=library_id,
                rel_path=rel_path,
                file_size=file_size,
                file_mtime=file_mtime_dt,
                media_type=media_type,
            )
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409, detail=f"Another file was just ingested at {rel_path}; scan again")
        asset_id = asset.asset_id
        created = True
    else:
        asset_id = existing.asset_id
        # Update file metadata if changed
        existing.file_size = file_size
        if file_mtime_dt is not None:
            existing.file_mtime = file_mtime_dt
        existing.media_type = media_type
        # A file the scanner marked missing has reappeared: restore it (search
        # finds it again: its words were kept). Without this, the asset stays
        # invisible (active_assets filters deleted_at) and the scanner
        # re-discovers it every cycle.
        if existing.deleted_at is not None:
            AssetRepository(session).clear_trash(existing)
        session.add(existing)

    from sqlalchemy.exc import IntegrityError

    try:
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
    except IntegrityError:
        # A clip brought back to this path (moved, or back where it was) while
        # another ingest put a clip here: one clip in sight holds a path.
        session.rollback()
        raise HTTPException(status_code=409, detail=f"Another file was just ingested at {rel_path}; scan again") from None
    def took_its_place() -> str | None:
        """The clip in sight at this path now, if it isn't the one just made:
        a clip that went missing with this content moved onto it (copy, then
        delete), so that's the clip here."""
        here = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
        return here.asset_id if here is not None and here.deleted_at is None and here.asset_id != asset_id else None

    # Another ingest's handover may already have moved a missing clip onto
    # this new one (a copy and the overwritten original in one scan).
    handed_to = took_its_place() if created and asset_repo.get_by_id(asset_id) is None else None
    if facet_data is not None and handed_to is None:
        from src.server.repository.lineage import record as record_lineage

        try:
            asset_repo.upsert_video_facet(asset_id, facet_data)
            record_lineage(session, asset_id, "probe", made["probe"])
        except IntegrityError:
            # Handed over to just now: the clip that moved here has its own.
            session.rollback()
            handed_to = took_its_place()
            if handed_to is None:
                raise
    if created and handed_to is None and content_hash and get_follow_moves(session):
        # A clip with this content may have gone missing while this one was
        # made (a copy and the overwritten original in one scan, ingested at
        # once): then this is its copy, and it moves here, as copy-then-delete.
        # Whichever ingest commits second sees the other. One clip at most:
        # the one that went last.
        from src.server.api.routers.trash import hand_over_to_copies

        went = asset_repo.missing_ids_with_hash(library_id, content_hash)[:1]
        if went and hand_over_to_copies(session, request, went):
            handed_to = took_its_place()
    if created and handed_to is None and asset_repo.get_by_id(asset_id) is None:
        handed_to = took_its_place()  # the other ingest's handover took it meanwhile
    if handed_to is not None:
        result.asset_id = handed_to
        created = False
    result.created = created
    return result


# ---------------------------------------------------------------------------
# POST /v1/assets/{asset_id}/ingest — ingest into existing asset
# ---------------------------------------------------------------------------


@router.post("/v1/assets/{asset_id}/ingest", response_model=IngestResponse, dependencies=[Depends(require_editor)])
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
    content_hash, _ = _exif_hashes(exif_data)
    vision_data = _parse_optional_json(vision, "vision")
    embeddings_data = _parse_optional_json_list(embeddings, "embeddings")
    made = _required_lineage(lineage, vision_data, embeddings_data, exif_data=exif_data)
    # A different file is a new clip (POST /v1/ingest makes it and archives
    # this one); this clip keeps the content its analysis was made from.
    if content_hash and asset.content_hash and content_hash != asset.content_hash:
        from src.server.api.errors import ConflictError

        raise ConflictError("different_file", "A different file is a new clip: ingest it with POST /v1/ingest",
                            {"asset_content_hash": asset.content_hash, "content_hash": content_hash})

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
