"""Assets API: upsert for scanner, trash and restore. All routes require tenant auth (middleware)."""

import base64
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlmodel import Session

from src.shared.io_utils import normalize_rel_path
from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_editor, require_signed_in
from src.server.api.errors import ConflictError, DecisionRequiredError
from src.shared import asset_status
from src.shared.io_utils import normalize_path_prefix
from src.server.repository.tenant import AssetMetadataRepository, AssetRepository, LibraryRepository
from src.server.models.tenant import Asset
from src.server.storage.local import get_storage
from src.shared.utils import utcnow

logger = logging.getLogger(__name__)



router = APIRouter(prefix="/v1/assets", tags=["assets"])


class UpsertAssetRequest(BaseModel):
    library_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    media_type: str
    force: bool = False


class UpsertAssetResponse(BaseModel):
    action: str  # added | updated | skipped


class VideoFacetModel(BaseModel):
    """One ffprobe pass over a video (see src/client/video/probe.py).

    Validated on the way in: a malformed value would break every export of
    every project that holds the clip.
    """

    duration_sec: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    container: str | None = None
    video_codec: str | None = None
    width: int | None = Field(default=None, gt=0)  # display width: rotation applied
    height: int | None = Field(default=None, gt=0)
    rotation: Literal[0, 90, 180, 270] = 0  # degrees clockwise to display upright
    frame_rate_num: int | None = Field(default=None, gt=0)
    frame_rate_den: int | None = Field(default=None, gt=0)
    # "HH:MM:SS:FF", or "HH:MM:SS;FF" for drop-frame
    start_timecode: str | None = Field(default=None, pattern=r"^\d{2}:[0-5]\d:[0-5]\d[:;]\d{2}$")
    drop_frame: bool | None = None
    audio_codec: str | None = None
    audio_channels: int | None = Field(default=None, ge=0)
    audio_sample_rate: int | None = Field(default=None, gt=0)


class AssetResponse(BaseModel):
    asset_id: str
    library_id: str
    rel_path: str
    media_type: str
    status: str
    proxy_key: str | None
    thumbnail_key: str | None
    width: int | None
    height: int | None
    # Source-file size in bytes. Included on the detail response (in
    # addition to the page response) so the lightbox can render it
    # regardless of how the user got there — e.g. opening from the
    # cluster review face drill-down, which only has a face_id +
    # asset_id and synthesizes a placeholder list-item with file_size=0
    # to satisfy the AssetPageItem shape.
    file_size: int | None = None
    sha256: str | None = None
    exif_extracted_at: str | None = None  # ISO8601
    camera_make: str | None = None
    camera_model: str | None = None
    taken_at: str | None = None  # ISO8601
    gps_lat: float | None = None
    gps_lon: float | None = None
    iso: int | None = None
    exposure_time_us: int | None = None
    aperture: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    lens_model: str | None = None
    flash_fired: bool | None = None
    orientation: int | None = None
    video_preview_key: str | None = None
    video_preview_generated_at: str | None = None  # ISO8601
    video_preview_last_accessed_at: str | None = None  # ISO8601
    duration_sec: float | None = None
    ai_description: str | None = None
    ai_tags: list[str] = []
    ocr_text: str | None = None
    transcript_srt: str | None = None
    transcript_language: str | None = None
    transcribed_at: str | None = None
    note: str | None = None
    note_author: str | None = None
    note_updated_at: str | None = None
    video_facet: VideoFacetModel | None = None


class AssetPageItem(BaseModel):
    """Asset fields returned by GET /v1/assets/page."""

    asset_id: str
    rel_path: str
    file_size: int
    file_mtime: str | None  # ISO8601
    sha256: str | None
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None  # ISO8601
    status: str = "pending"
    duration_sec: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    iso: int | None = None
    aperture: float | None = None
    focal_length: float | None = None
    focal_length_35mm: float | None = None
    lens_model: str | None = None
    flash_fired: bool | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    face_count: int | None = None
    created_at: str | None = None  # ISO8601
    has_analysis_proxy: bool = False


class AssetPageResponse(BaseModel):
    """Response envelope for paginated assets."""

    items: list[AssetPageItem]
    next_cursor: str | None = None


# Valid sort columns for the page endpoint.
SORT_COLUMNS = {"taken_at", "created_at", "file_size", "iso", "exposure_time_us", "aperture", "focal_length", "rel_path", "asset_id"}


def _encode_cursor(sort_col: str, sort_value: object, asset_id: str) -> str:
    """Encode a composite cursor as base64 JSON."""
    payload = json.dumps({"v": sort_value, "id": asset_id}, default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class BatchTrashRequest(BaseModel):
    asset_ids: list[str]
    # "user": a person trashed them; deleted for good after the trash days,
    # and scans leave them trashed. "missing": the scanner no longer finds the
    # files (archived; restored when they reappear). Omitted = "missing": what
    # scanners sent before reasons existed (the Mac app still does).
    reason: Literal["user", "missing"] | None = None
    # A person's trash: required when any of the clips are in projects. In the
    # trash they're hidden there, and deleted for good they leave them.
    remove_from_projects: bool = False


class PickClipsRequest(BaseModel):
    """Clips by id, or every clip under a folder of a library (path "" is the
    whole library). The folder only picks the clips; it has no state of its own."""

    asset_ids: list[str] | None = Field(default=None, max_length=10_000)
    library_id: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _one_way(self) -> "PickClipsRequest":
        by_folder = self.library_id is not None or self.path is not None
        if (self.asset_ids is not None) == by_folder:
            raise ValueError("Send asset_ids, or library_id and path")
        if by_folder and (self.library_id is None or self.path is None):
            raise ValueError("A folder needs library_id and path")
        return self


class ArchiveResponse(BaseModel):
    archived: list[str]
    # Asked for but not in sight (already archived, in the trash, or unknown): left as they were.
    skipped: list[str] = []


class UnarchiveResponse(BaseModel):
    unarchived: list[str]
    # Not archived by a person (a missing file's clip comes back with its file), or unknown.
    skipped: list[str] = []


class RestoreRequest(BaseModel):
    asset_ids: list[str] = Field(max_length=10_000)


class RestoreResponse(BaseModel):
    restored: list[str]
    # Not in a person's trash (archived, missing, in sight, unknown), or their library is in the trash.
    skipped: list[str] = []



class BatchTrashResponse(BaseModel):
    trashed: list[str]
    not_found: list[str]
    # Of the trashed, those that moved to an empty copy of their file instead
    # (copy, then delete the original): active again, at the copy's path.
    handed_over: list[str] = []


class StateCheckRequest(BaseModel):
    asset_ids: list[str]

    @field_validator("asset_ids")
    @classmethod
    def max_ids(cls, v: list[str]) -> list[str]:
        if len(v) == 0:
            raise ValueError("asset_ids must not be empty")
        if len(v) > 500:
            raise ValueError("Maximum 500 asset_ids per request")
        return v


class AssetStateItem(BaseModel):
    asset_id: str
    deleted: bool
    proxy_sha256: str | None


class StateCheckResponse(BaseModel):
    assets: list[AssetStateItem]


class VisionSubmitRequest(BaseModel):
    model_id: str
    model_version: str = "1"
    description: str
    tags: list[str] = []
    client_proxy_sha256: str | None = None


class VisionSubmitResponse(BaseModel):
    asset_id: str
    status: str


@router.get("/page", response_model=AssetPageResponse)
def page_assets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    after: str | None = None,
    limit: int = 500,
    path_prefix: str | None = None,
    tag: str | None = None,
    missing_vision: bool = False,
    missing_embeddings: bool = False,
    missing_faces: bool = False,
    missing_face_embeddings: bool = False,
    missing_video_scenes: bool = False,
    missing_ocr: bool = False,
    missing_scene_vision: bool = False,
    missing_transcription: bool = False,
    missing_probe: bool = False,
    missing_analysis_proxy: bool = False,
    has_faces: bool | None = None,
    person_id: str | None = None,
    sort: str = "taken_at",
    dir: str = "desc",
    media_type: str | None = None,
    camera_make: str | None = None,
    camera_model: str | None = None,
    lens_model: str | None = None,
    iso_min: int | None = None,
    iso_max: int | None = None,
    exposure_min_us: int | None = None,
    exposure_max_us: int | None = None,
    aperture_min: float | None = None,
    aperture_max: float | None = None,
    focal_length_min: float | None = None,
    focal_length_max: float | None = None,
    has_exposure: bool | None = None,
    has_gps: bool = False,
    near_lat: float | None = None,
    near_lon: float | None = None,
    near_radius_km: float = 1.0,
    favorite: bool | None = None,
    star_min: int | None = None,
    star_max: int | None = None,
    color: str | None = None,
    has_rating: bool | None = None,
) -> AssetPageResponse:
    """
    Keyset-paginated assets with sorting and filtering.
    Returns a response envelope with items and next_cursor.
    """
    if limit > 500:
        limit = 500
    if limit < 1:
        limit = 1
    if getattr(request.state, "is_public_request", False):
        lib_repo = LibraryRepository(session)
        library = lib_repo.get_by_id(library_id)
        if library is None or not library.is_public:
            raise HTTPException(status_code=404, detail="Not found")
        # Ratings are a signed-in person's; who's in a photo isn't for visitors to probe.
        if person_id or any(v is not None for v in (favorite, star_min, star_max, color, has_rating)):
            raise HTTPException(status_code=403, detail="That filter isn't available on public pages")

    sort_col = sort if sort in SORT_COLUMNS else "taken_at"
    direction = dir if dir in ("asc", "desc") else "desc"

    asset_repo = AssetRepository(session)
    normalized_prefix: str | None = None
    if path_prefix is not None:
        normalized_prefix = normalize_path_prefix(path_prefix)
        if normalized_prefix and ".." in normalized_prefix.split("/"):
            raise HTTPException(
                status_code=400,
                detail="Invalid path_prefix; path traversal not allowed",
            )

    media_types: list[str] | None = None
    if media_type:
        media_types = [m.strip() for m in media_type.split(",") if m.strip()]

    # Rating filters require user identity
    rating_user_id: str | None = None
    color_list: list[str] | None = None
    needs_rating_filter = favorite is not None or star_min is not None or star_max is not None or color is not None or has_rating is not None
    if needs_rating_filter:
        uid = getattr(request.state, "user_id", None)
        if not uid:
            key_id = getattr(request.state, "key_id", None)
            rating_user_id = f"key:{key_id}" if key_id else None
        else:
            rating_user_id = uid
        if color is not None:
            color_list = [c.strip() for c in color.split(",") if c.strip()]

    assets = asset_repo.page_by_library(
        library_id=library_id,
        after=after,
        limit=limit,
        path_prefix=normalized_prefix,
        tag=tag,
        missing_vision=missing_vision,
        missing_embeddings=missing_embeddings,
        missing_faces=missing_faces,
        missing_face_embeddings=missing_face_embeddings,
        missing_video_scenes=missing_video_scenes,
        missing_ocr=missing_ocr,
        missing_scene_vision=missing_scene_vision,
        missing_transcription=missing_transcription,
        missing_probe=missing_probe,
        missing_analysis_proxy=missing_analysis_proxy,
        has_faces=has_faces,
        person_id=person_id,
        sort=sort_col,
        direction=direction,
        media_types=media_types,
        camera_make=camera_make,
        camera_model=camera_model,
        lens_model=lens_model,
        iso_min=iso_min,
        iso_max=iso_max,
        exposure_min_us=exposure_min_us,
        exposure_max_us=exposure_max_us,
        aperture_min=aperture_min,
        aperture_max=aperture_max,
        focal_length_min=focal_length_min,
        focal_length_max=focal_length_max,
        has_exposure=has_exposure,
        has_gps=has_gps,
        near_lat=near_lat,
        near_lon=near_lon,
        near_radius_km=near_radius_km,
        rating_user_id=rating_user_id,
        favorite=favorite,
        star_min=star_min,
        star_max=star_max,
        color=color_list,
        has_rating=has_rating,
    )

    items = [
        AssetPageItem(
            asset_id=a.asset_id,
            rel_path=a.rel_path,
            file_size=a.file_size,
            file_mtime=a.file_mtime.isoformat() if a.file_mtime else None,
            sha256=a.sha256,
            media_type=a.media_type,
            width=a.width,
            height=a.height,
            taken_at=a.taken_at.isoformat() if a.taken_at else None,
            status=a.status,
            duration_sec=a.duration_sec,
            camera_make=a.camera_make,
            camera_model=a.camera_model,
            iso=a.iso,
            aperture=a.aperture,
            focal_length=a.focal_length,
            focal_length_35mm=a.focal_length_35mm,
            lens_model=a.lens_model,
            flash_fired=a.flash_fired,
            gps_lat=a.gps_lat,
            gps_lon=a.gps_lon,
            face_count=a.face_count,
            created_at=a.created_at.isoformat() if a.created_at else None,
            has_analysis_proxy=a.analysis_proxy_key is not None,
        )
        for a in assets
    ]

    next_cursor: str | None = None
    if items and len(items) == limit:
        last = assets[-1]
        sort_value = getattr(last, sort_col, None)
        if hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)

    return AssetPageResponse(items=items, next_cursor=next_cursor)


class RepairSummary(BaseModel):
    total_assets: int = 0
    missing_proxy: int = 0
    missing_exif: int = 0
    missing_vision: int = 0
    missing_embeddings: int = 0
    missing_faces: int = 0
    missing_face_embeddings: int = 0
    missing_ocr: int = 0
    missing_video_scenes: int = 0
    missing_scene_vision: int = 0
    missing_transcription: int = 0
    missing_probe: int = 0
    missing_analysis_proxy: int = 0
    stale_search_sync: int = 0


@router.get("/repair-summary", response_model=RepairSummary, dependencies=[Depends(require_signed_in)])
def repair_summary(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
) -> RepairSummary:
    """Count assets missing various pipeline outputs for a library."""
    from sqlalchemy import text
    lib = LibraryRepository(session).get_by_id(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="Library not found")
    from src.server.repository.tenant import MISSING_CONDITIONS
    row = session.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE proxy_key IS NULL) AS missing_proxy,
                COUNT(*) FILTER (WHERE exif_extracted_at IS NULL AND media_type = 'image') AS missing_exif,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_vision"]}) AS missing_vision,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_embeddings"]}) AS missing_embeddings,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_faces"]}) AS missing_faces,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_face_embeddings"]}) AS missing_face_embeddings,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_ocr"]}) AS missing_ocr,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_video_scenes"]}) AS missing_video_scenes,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_scene_vision"]}) AS missing_scene_vision,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_transcription"]}) AS missing_transcription,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_probe"]}) AS missing_probe,
                COUNT(*) FILTER (WHERE {MISSING_CONDITIONS["missing_analysis_proxy"]}) AS missing_analysis_proxy,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM asset_metadata am
                        WHERE am.asset_id = a.asset_id
                    ) AND (
                        a.search_synced_at IS NULL
                        OR a.search_synced_at < (
                            SELECT MAX(am2.generated_at)
                            FROM asset_metadata am2
                            WHERE am2.asset_id = a.asset_id
                        )
                    )
                ) AS stale_search_sync
            FROM active_assets a
            WHERE library_id = :library_id
        """),
        {"library_id": library_id},
    ).one()
    return RepairSummary(
        total_assets=row.total,
        missing_proxy=row.missing_proxy,
        missing_exif=row.missing_exif,
        missing_vision=row.missing_vision,
        missing_embeddings=row.missing_embeddings,
        missing_faces=row.missing_faces,
        missing_face_embeddings=row.missing_face_embeddings,
        missing_ocr=row.missing_ocr,
        missing_video_scenes=row.missing_video_scenes,
        missing_scene_vision=row.missing_scene_vision,
        missing_transcription=row.missing_transcription,
        missing_probe=row.missing_probe,
        missing_analysis_proxy=row.missing_analysis_proxy,
        stale_search_sync=row.stale_search_sync,
    )


@router.post("/state-check", response_model=StateCheckResponse)
def state_check(
    body: StateCheckRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StateCheckResponse:
    """Return deletion status and proxy_sha256 for a batch of asset IDs.

    Includes soft-deleted assets (client needs to evict them from cache).
    Asset IDs not found in the DB are returned with deleted=True, proxy_sha256=None.
    Maximum 500 IDs per request.
    """
    states = AssetRepository(session).get_states(body.asset_ids)
    items = [
        AssetStateItem(
            asset_id=aid,
            deleted=states[aid]["deleted"] if aid in states else True,
            proxy_sha256=states[aid]["proxy_sha256"] if aid in states else None,
        )
        for aid in body.asset_ids
    ]
    return StateCheckResponse(assets=items)


def _stream_asset_file(
    asset_id: str,
    size: str,  # "proxy" or "thumbnail"
    request: Request,
    session: Session,
) -> StreamingResponse:
    # Signed in, a clip out of sight (archived, in the trash) still shows its
    # pictures: the archive and trash views need them. Public pages never do.
    asset = session.get(Asset, asset_id)
    is_public = getattr(request.state, "is_public_request", False)
    signed_in = getattr(request.state, "role", None) in ("admin", "editor", "viewer")
    if asset is None or (asset.deleted_at is not None and (is_public or not signed_in)):
        raise HTTPException(status_code=404, detail="Asset not found")
    if is_public:
        public_library_id = request.query_params.get("public_library_id")
        public_project_id = request.query_params.get("public_project_id") or request.query_params.get(
            "public_collection_id"  # pre-rename name
        )
        if public_library_id:
            if asset.library_id != public_library_id:
                raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
            lib = LibraryRepository(session).get_by_id(public_library_id)
            if lib is None or not lib.is_public:
                raise HTTPException(status_code=404, detail="Not found")
        elif public_project_id:
            from src.server.repository.tenant import ProjectRepository, ProjectAsset
            from src.server.models.tenant import Project
            col_repo = ProjectRepository(session)
            col = col_repo.get_by_id(public_project_id)
            if col is None or col.visibility != "public":
                raise HTTPException(status_code=404, detail="Not found")
            # Verify asset is in this project
            from sqlmodel import select
            membership = session.exec(
                select(ProjectAsset).where(
                    ProjectAsset.project_id == public_project_id,
                    ProjectAsset.asset_id == asset_id,
                )
            ).first()
            if membership is None:
                raise HTTPException(status_code=403, detail="Asset not in project")
        else:
            raise HTTPException(status_code=403, detail="Public access requires library or project context")

    key = asset.proxy_key if size == "proxy" else asset.thumbnail_key
    if not key:
        raise HTTPException(
            status_code=404,
            detail=f"No {size} available for this asset",
        )

    storage = get_storage()
    path = storage.abs_path(key)
    if not path.exists():
        # Stale key: file was lost. Clear it so next ingest regenerates it.
        if size == "proxy":
            asset.proxy_key = None
        else:
            asset.thumbnail_key = None
        session.add(asset)
        session.commit()
        raise HTTPException(status_code=404, detail=f"No {size} available for this asset")

    key_ext = Path(key).suffix.lower()
    if key_ext == ".webp":
        content_type = "image/webp"
        filename = Path(asset.rel_path).stem + ".webp"
    else:
        content_type = "image/jpeg"
        filename = Path(asset.rel_path).stem + ".jpg"

    # HTTP headers must be latin-1 encodable. macOS screenshot filenames
    # contain \u202f (narrow no-break space) which is not latin-1 safe.
    # Use RFC 5987 filename* for the full UTF-8 name, and a sanitized
    # ASCII fallback for the plain filename.
    from urllib.parse import quote
    ascii_filename = filename.encode("ascii", errors="replace").decode("ascii")
    utf8_filename = quote(filename)

    def _iter() -> bytes:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(),
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{utf8_filename}"
            ),
        },
    )


def _stream_file_with_range(
    path: Path,
    request: Request,
    media_type: str,
    etag: str | None = None,
) -> StreamingResponse:
    """Stream `path`, honoring a single Range: `bytes=a-b`, `bytes=a-` or `bytes=-n`.

    A range starting past the end is a 416; a malformed one gets the whole
    file, and so does one whose If-Range names another version (`etag`).
    """
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1
    status_code = 200
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = etag

    if_range = request.headers.get("if-range")
    if range_header and if_range and etag and if_range.strip() != etag:
        range_header = None  # the client holds another version: send this one whole
    if range_header:
        units, _, range_spec = range_header.partition("=")
        start_str, _, end_str = range_spec.split(",")[0].strip().partition("-")
        try:
            if units.strip().lower() != "bytes":
                raise ValueError(units)
            if start_str:
                start = int(start_str)
                if end_str:
                    end = min(int(end_str), file_size - 1)
            else:  # the last n bytes
                start = max(file_size - int(end_str), 0)
            if start >= file_size:
                return StreamingResponse(
                    iter(()), status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{file_size}"},
                )
            if start > end:
                raise ValueError(range_header)
            status_code = 206
        except ValueError:
            start, end, status_code = 0, file_size - 1, 200

    content_length = end - start + 1
    headers["Content-Length"] = str(content_length)
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    def file_iterator() -> bytes:
        with open(path, "rb") as f:
            f.seek(start)
            remaining = content_length
            chunk_size = 65536
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                chunk = f.read(read_size)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        file_iterator(),
        media_type=media_type,
        status_code=status_code,
        headers=headers,
    )


def _check_public_request(request: Request, session: Session, asset) -> None:
    """For a public page's request, raise unless its library or project shows this asset."""
    if not getattr(request.state, "is_public_request", False):
        return
    from src.server.api.routers.playback import _check_public

    q = request.query_params
    _check_public(session, asset, q.get("public_library_id"),
                  q.get("public_project_id") or q.get("public_collection_id"))


def _project_visitor_view(response: AssetResponse) -> AssetResponse:
    """What a public project's page may show of a clip: what its clip list gives
    (shape, time, length) and what's seen or heard in it. Not where it lives,
    where it was shot, what shot it, or the team's notes."""
    return AssetResponse(
        asset_id=response.asset_id,
        library_id="",
        rel_path="",
        media_type=response.media_type,
        status=response.status,
        proxy_key=None,
        thumbnail_key=None,
        width=response.width,
        height=response.height,
        taken_at=response.taken_at,
        duration_sec=response.duration_sec,
        ai_description=response.ai_description,
        ai_tags=response.ai_tags,
        ocr_text=response.ocr_text,
        transcript_srt=response.transcript_srt,
        transcript_language=response.transcript_language,
        video_facet=response.video_facet,
    )


def _trim_public_transcript(request: Request, session: Session, response: AssetResponse) -> None:
    """A public page's transcript stops where its playback does, and it shows no notes:
    they're the team's working notes (Robert's call, Oct 8)."""
    if not getattr(request.state, "is_public_request", False):
        return
    response.note = None
    response.note_author = None
    response.note_updated_at = None
    if not response.transcript_srt:
        return
    from src.server.api.routers.playback import srt_before
    from src.server.tenant_settings import playback_cap

    cap = playback_cap(session, public=True)
    if cap is not None:
        response.transcript_srt = srt_before(response.transcript_srt, cap)


def _to_asset_response(asset) -> AssetResponse:
    """Map an Asset ORM/model object to AssetResponse."""
    return AssetResponse(
        asset_id=asset.asset_id,
        library_id=asset.library_id,
        rel_path=asset.rel_path,
        media_type=asset.media_type,
        status=asset.status,
        proxy_key=asset.proxy_key,
        thumbnail_key=asset.thumbnail_key,
        width=asset.width,
        height=asset.height,
        file_size=asset.file_size,
        sha256=asset.sha256,
        exif_extracted_at=asset.exif_extracted_at.isoformat() if asset.exif_extracted_at else None,
        camera_make=asset.camera_make,
        camera_model=asset.camera_model,
        taken_at=asset.taken_at.isoformat() if asset.taken_at else None,
        gps_lat=asset.gps_lat,
        gps_lon=asset.gps_lon,
        iso=asset.iso,
        exposure_time_us=asset.exposure_time_us,
        aperture=asset.aperture,
        focal_length=asset.focal_length,
        focal_length_35mm=asset.focal_length_35mm,
        lens_model=asset.lens_model,
        flash_fired=asset.flash_fired,
        orientation=asset.orientation,
        video_preview_key=asset.video_preview_key,
        video_preview_generated_at=asset.video_preview_generated_at.isoformat()
        if asset.video_preview_generated_at
        else None,
        video_preview_last_accessed_at=asset.video_preview_last_accessed_at.isoformat()
        if asset.video_preview_last_accessed_at
        else None,
        duration_sec=asset.duration_sec,
        transcript_srt=asset.transcript_srt,
        transcript_language=asset.transcript_language,
        transcribed_at=asset.transcribed_at.isoformat() if asset.transcribed_at else None,
        note=asset.note,
        note_author=asset.note_author,
        note_updated_at=asset.note_updated_at.isoformat() if asset.note_updated_at else None,
    )


@router.get("/{asset_id}/proxy")
def stream_proxy(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StreamingResponse:
    """Stream the proxy JPEG for an asset."""
    return _stream_asset_file(asset_id, "proxy", request, session)


@router.get("/{asset_id}/thumbnail")
def stream_thumbnail(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StreamingResponse:
    """Stream the thumbnail JPEG for an asset."""
    return _stream_asset_file(asset_id, "thumbnail", request, session)


@router.get("/by-path", response_model=AssetResponse)
def get_asset_by_path(
    library_id: str,
    rel_path: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AssetResponse:
    """Return a single asset by library_id + rel_path. 404 if not found or trashed."""
    rel_path = normalize_rel_path(rel_path)
    if getattr(request.state, "is_public_request", False):
        lib = LibraryRepository(session).get_by_id(library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {rel_path}")
    response = _to_asset_response(asset)
    ai_description: str | None = None
    ai_tags: list[str] = []
    ocr_text: str | None = None

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []
        ocr_text = meta.data.get("ocr_text") or None

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    response.ocr_text = ocr_text
    facet = AssetRepository(session).get_video_facet(asset.asset_id)
    # Stored rows are returned as they are (validation is for writes).
    response.video_facet = VideoFacetModel.model_construct(**facet) if facet else None
    _trim_public_transcript(request, session, response)
    return response


@router.get("", response_model=list[AssetResponse])
def list_assets(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
) -> list[AssetResponse]:
    """List active (non-trashed) assets, optionally filtered by library_id."""
    asset_repo = AssetRepository(session)
    assets = asset_repo.list_by_library(library_id) if library_id else asset_repo.list_all()
    return [_to_asset_response(a) for a in assets]


class ProjectUsageRequest(BaseModel):
    asset_ids: list[str] = []
    # Every clip in these libraries, e.g. before emptying the library trash.
    library_ids: list[str] = []


class ProjectUsageItem(BaseModel):
    project_id: str
    name: str
    status: str  # active | archived
    in_trash: bool
    clips: int  # how many of the asked-about clips it holds


class ProjectUsageResponse(BaseModel):
    assets_in_projects: int  # how many of the clips are in any project
    projects: list[ProjectUsageItem]  # the ones the caller can see
    other_projects: int  # the rest: counted, not named


@router.post("/project-usage", response_model=ProjectUsageResponse)
def project_usage(
    body: ProjectUsageRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ProjectUsageResponse:
    """Which projects hold these clips, or any clip in these libraries: what
    deleting them for good would take them out of. Every project counts,
    archived, trashed and other people's included; names only for those the
    caller can see."""
    return project_usage_summary(session, user_id, asset_ids=body.asset_ids, library_ids=body.library_ids)


def project_usage_summary(
    session: Session, user_id: str | None, *, asset_ids: list[str] = (), library_ids: list[str] = ()  # type: ignore[assignment]
) -> ProjectUsageResponse:
    """See project_usage. Also what a permanent delete refuses with until
    the request says remove_from_projects."""
    from src.server.repository.tenant import ProjectRepository

    rows, in_any = ProjectRepository(session).usage(list(asset_ids), list(library_ids))
    visible: list[ProjectUsageItem] = []
    other = 0
    for project, clips in rows:
        mine = project.owner_user_id in (None, user_id)
        shown = mine or (project.deleted_at is None and project.visibility in ("shared", "public"))
        if not shown:
            other += 1
            continue
        visible.append(ProjectUsageItem(
            project_id=project.project_id, name=project.name, status=project.status,
            in_trash=project.deleted_at is not None, clips=clips,
        ))
    return ProjectUsageResponse(assets_in_projects=in_any, projects=visible, other_projects=other)


def _ask_about_projects(session: Session, request: Request, asset_ids: list[str]) -> None:
    """409 in_projects when a person trashes clips that projects use: hidden
    there while in the trash, and gone from them once deleted for good."""
    usage = project_usage_summary(session, get_current_user_id(request), asset_ids=asset_ids)
    if usage.assets_in_projects:
        n = usage.assets_in_projects
        raise DecisionRequiredError(
            "in_projects",
            f"{n} of these clips {'is' if n == 1 else 'are'} in projects. In the trash they're hidden "
            "there; deleted for good, they leave those projects. Send remove_from_projects: true to go ahead.",
            usage.model_dump(),
        )


def _out_of_search(background: BackgroundTasks, request: Request, asset_ids: list[str]) -> None:
    """Drop these clips' search documents after the response (best effort):
    search already leaves out clips out of sight; this keeps pages full."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset_ids:
        from src.server.search.quickwit_client import QuickwitClient

        background.add_task(QuickwitClient().delete_tenant_documents_by_asset_ids, tenant_id, list(asset_ids))


def _back_in_search(background: BackgroundTasks, request: Request, session: Session, asset_ids: list[str]) -> None:
    """Clips back in sight: the sync sweep re-indexes them and their scenes
    (bringing them back queued them); transcript segments are put back here,
    after the response."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id or not asset_ids:
        return
    from sqlmodel import select

    with_transcripts = list(session.exec(
        select(Asset).where(Asset.asset_id.in_(asset_ids), Asset.transcript_srt.is_not(None))  # type: ignore[attr-defined,union-attr]
    ).all())
    if with_transcripts:
        from src.server.search.sync import index_transcript_segments

        def run() -> None:
            for asset in with_transcripts:
                index_transcript_segments(tenant_id, asset)

        background.add_task(run)


def _refresh_grids(session: Session, asset_ids: list[str]) -> None:
    """Bump the revision of every library these clips are in, so open grids and folder trees reload."""
    asset_repo, library_repo = AssetRepository(session), LibraryRepository(session)
    for library_id in asset_repo.library_ids_of(asset_ids):
        library_repo.bump_revision(library_id)


@router.delete("", response_model=BatchTrashResponse)
def batch_trash_assets(
    body: BatchTrashRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> BatchTrashResponse:
    """Take clips out of sight: a person's trash ("user"), or files a scan no longer finds ("missing").

    A person's trash needs an editor, takes archived clips too (deleting an
    archived clip moves it to the trash), and asks first about clips that
    projects use (409 in_projects). Marking files missing stays open to
    whoever can scan, and hands each over to an empty copy of its file when
    there is one (follow moves).
    """
    reason = body.reason or "missing"
    if reason == "user":
        require_editor(request)
        if not body.remove_from_projects:
            _ask_about_projects(session, request, body.asset_ids)
    asset_repo = AssetRepository(session)
    trashed_ids, not_found_ids = asset_repo.trash_many(body.asset_ids, reason=reason)
    _out_of_search(background, request, trashed_ids)
    handed_over: list[str] = []
    if trashed_ids and reason == "missing":
        # Copy, then delete the original: the asset moves to its empty copy.
        from src.server.api.routers.trash import hand_over_to_copies
        from src.server.tenant_settings import get_follow_moves

        if get_follow_moves(session):
            handed_over = hand_over_to_copies(session, request, trashed_ids)
    if trashed_ids and reason == "user":
        _refresh_grids(session, trashed_ids)
    return BatchTrashResponse(trashed=trashed_ids, not_found=not_found_ids, handed_over=handed_over)


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    public_library_id: str | None = None,
) -> AssetResponse:
    """Return a single asset by id. 404 if not found or trashed."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    _check_public_request(request, session, asset)
    via_project = getattr(request.state, "is_public_request", False) and bool(
        request.query_params.get("public_project_id") or request.query_params.get("public_collection_id")
    )
    response = _to_asset_response(asset)
    ai_description: str | None = None
    ai_tags: list[str] = []
    ocr_text: str | None = None

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset.asset_id)
    if meta and meta.data:
        ai_description = meta.data.get("description") or None
        ai_tags = meta.data.get("tags") or []
        ocr_text = meta.data.get("ocr_text") or None

    response.ai_description = ai_description
    response.ai_tags = ai_tags
    response.ocr_text = ocr_text
    facet = AssetRepository(session).get_video_facet(asset.asset_id)
    # Stored rows are returned as they are (validation is for writes).
    response.video_facet = VideoFacetModel.model_construct(**facet) if facet else None
    _trim_public_transcript(request, session, response)
    return _project_visitor_view(response) if via_project else response


@router.put("/{asset_id}/video-facet", response_model=VideoFacetModel)
def put_video_facet(
    asset_id: str,
    body: VideoFacetModel,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> VideoFacetModel:
    """Store a video's probe result (replaces any earlier one). Sets duration_sec."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Video facets are only for video assets")
    asset_repo.upsert_video_facet(asset_id, body.model_dump())
    LibraryRepository(session).bump_revision(asset.library_id)
    return body


@router.delete("/{asset_id}", status_code=204)
def trash_asset(
    asset_id: str,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    remove_from_projects: bool = False,
) -> None:
    """A person trashes one clip (in sight or archived); scans leave it trashed.

    404 if not found, already in the trash, or trashed with its library.
    409 in_projects as for DELETE /v1/assets.
    """
    if not remove_from_projects:
        _ask_about_projects(session, request, [asset_id])
    if not AssetRepository(session).trash(asset_id, reason="user"):
        raise HTTPException(status_code=404, detail="Asset not found or already trashed")
    _out_of_search(background, request, [asset_id])
    _refresh_grids(session, [asset_id])


def reindex_restored_asset(request: Request, asset: Asset) -> None:
    """Put a restored asset's transcript segments back in search; the sync
    sweep re-indexes the asset and its scenes (restore queued them)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset.transcript_srt:
        from src.server.search.sync import index_transcript_segments

        index_transcript_segments(tenant_id, asset)


_NOT_THE_TRASH = {
    "archived": ("archived", "This clip is archived, not in the trash: unarchive it (POST /v1/assets/unarchive)."),
    "missing": ("file_missing", "This clip's file is missing; it comes back when the file does."),
    "library_trashed": ("library_trashed", "This clip went to the trash with its library; restore the library."),
}


@router.post("/{asset_id}/restore", status_code=204)
def restore_asset(
    asset_id: str,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> None:
    """Take one clip out of the trash. 404 if not found or not deleted;
    409 archived, file_missing or library_trashed for a clip that isn't in a
    person's trash."""
    outcome = AssetRepository(session).restore(asset_id)
    if outcome in _NOT_THE_TRASH:
        raise ConflictError(*_NOT_THE_TRASH[outcome])
    if outcome != "restored":
        raise HTTPException(status_code=404, detail="Asset not found or not trashed")
    _back_in_search(background, request, session, [asset_id])
    _refresh_grids(session, [asset_id])


@router.post("/restore", response_model=RestoreResponse)
def restore_assets(
    body: RestoreRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> RestoreResponse:
    """Take clips out of the trash. Anything else asked for is skipped:
    archived or missing clips, clips in sight, and clips whose library is in the trash."""
    restored, skipped = AssetRepository(session).restore_many(body.asset_ids)
    _back_in_search(background, request, session, restored)
    _refresh_grids(session, restored)
    return RestoreResponse(restored=restored, skipped=skipped)


def _folder_library(session: Session, library_id: str) -> None:
    library = LibraryRepository(session).get_by_id(library_id)
    if library is None or library.status == "trashed":
        raise HTTPException(status_code=404, detail="Library not found")


@router.post("/archive", response_model=ArchiveResponse)
def archive_assets(
    body: PickClipsRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> ArchiveResponse:
    """Archive clips: out of sight, kept forever, with everything they have.
    Scans leave them archived while their files are on disk; unarchive brings
    them back. By id (clips not in sight are skipped) or every clip in sight
    under a folder."""
    repo = AssetRepository(session)
    if body.asset_ids is not None:
        archived, skipped = repo.archive(body.asset_ids)
    else:
        _folder_library(session, body.library_id)  # type: ignore[arg-type]
        archived, skipped = repo.archive_folder(body.library_id, body.path), []  # type: ignore[arg-type]
    _out_of_search(background, request, archived)
    _refresh_grids(session, archived)
    return ArchiveResponse(archived=archived, skipped=skipped)


@router.post("/unarchive", response_model=UnarchiveResponse)
def unarchive_assets(
    body: PickClipsRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> UnarchiveResponse:
    """Bring back clips a person archived, by id or under a folder. A missing
    file's clip isn't brought back: it returns when its file does."""
    repo = AssetRepository(session)
    if body.asset_ids is not None:
        back, skipped = repo.unarchive(body.asset_ids)
    else:
        _folder_library(session, body.library_id)  # type: ignore[arg-type]
        back, skipped = repo.unarchive_folder(body.library_id, body.path), []  # type: ignore[arg-type]
    _back_in_search(background, request, session, back)
    _refresh_grids(session, back)
    return UnarchiveResponse(unarchived=back, skipped=skipped)


@router.post("/{asset_id}/vision", response_model=VisionSubmitResponse)
def submit_vision(
    asset_id: str,
    body: VisionSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> VisionSubmitResponse:
    """Submit AI vision results for an asset with optional proxy hash validation.

    If client_proxy_sha256 is provided and the server has a stored hash, they must
    match — otherwise 409. If the server hash is null (pre-Phase 1 or no proxy yet),
    the check is skipped for backwards compatibility.
    """
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    if body.client_proxy_sha256 is not None and asset.proxy_sha256 is not None:
        if body.client_proxy_sha256 != asset.proxy_sha256:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "proxy_hash_mismatch",
                        "message": "Client proxy does not match server proxy. Re-download the proxy and retry.",
                    }
                },
            )

    AssetMetadataRepository(session).upsert(
        asset_id=asset_id,
        model_id=body.model_id,
        model_version=body.model_version,
        data={"description": body.description, "tags": body.tags},
    )
    AssetRepository(session).set_status(asset_id, asset_status.DESCRIBED)

    # Inline search sync (best-effort)
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    if meta:
        from src.server.search.sync import try_sync_asset
        try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    # Bump library revision for UI polling
    LibraryRepository(session).bump_revision(asset.library_id)

    return VisionSubmitResponse(asset_id=asset_id, status="described")


class OcrSubmitRequest(BaseModel):
    ocr_text: str


@router.post("/{asset_id}/ocr", status_code=200)
def submit_ocr(
    asset_id: str,
    body: OcrSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit OCR text for an asset. Merges into existing metadata."""
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    meta_repo = AssetMetadataRepository(session)
    meta = meta_repo.get_latest(asset_id=asset_id)
    if meta is None:
        raise HTTPException(status_code=400, detail="Asset has no vision metadata — run vision first")

    # Merge ocr_text + has_text into existing metadata data dict
    data = dict(meta.data) if meta.data else {}
    data["ocr_text"] = body.ocr_text
    data["has_text"] = bool(body.ocr_text)
    meta_repo.upsert(
        asset_id=asset_id,
        model_id=meta.model_id,
        model_version=meta.model_version,
        data=data,
    )

    # Re-sync search
    meta = meta_repo.get_latest(asset_id=asset_id)
    from src.server.search.sync import try_sync_asset
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return {"asset_id": asset_id, "ocr_text": body.ocr_text}


class BatchOcrItem(BaseModel):
    asset_id: str
    ocr_text: str


class BatchOcrRequest(BaseModel):
    items: list[BatchOcrItem]


@router.post("/batch-ocr", status_code=200)
def submit_batch_ocr(
    body: BatchOcrRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit OCR text for multiple assets in one request."""
    meta_repo = AssetMetadataRepository(session)
    asset_repo = AssetRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        meta = meta_repo.get_latest(asset_id=item.asset_id)
        if meta is None:
            skipped += 1
            continue
        data = dict(meta.data) if meta.data else {}
        data["ocr_text"] = item.ocr_text
        data["has_text"] = bool(item.ocr_text)
        meta_repo.upsert(
            asset_id=item.asset_id,
            model_id=meta.model_id,
            model_version=meta.model_version,
            data=data,
        )
        updated += 1

    # Clear search_synced_at so the sweep picks these up for re-indexing.
    # Avoids 25 individual Quickwit calls inside the request.
    if updated > 0:
        from sqlalchemy import text
        asset_ids = [item.asset_id for item in body.items]
        session.execute(
            text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = ANY(:ids)"),
            {"ids": asset_ids},
        )
        session.commit()

        # Bump revision once per library
        lib_ids = {a.library_id for item in body.items if (a := asset_repo.get_by_id(item.asset_id))}
        lib_repo = LibraryRepository(session)
        for lid in lib_ids:
            lib_repo.bump_revision(lid)

    return {"updated": updated, "skipped": skipped}


class BatchVisionItem(BaseModel):
    asset_id: str
    model_id: str
    model_version: str = "1"
    description: str
    tags: list[str] = []


class BatchVisionRequest(BaseModel):
    items: list[BatchVisionItem]


@router.post("/batch-vision", status_code=200)
def submit_batch_vision(
    body: BatchVisionRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit AI vision results for multiple assets in one request."""
    meta_repo = AssetMetadataRepository(session)
    asset_repo = AssetRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        meta_repo.upsert(
            asset_id=item.asset_id,
            model_id=item.model_id,
            model_version=item.model_version,
            data={"description": item.description, "tags": item.tags},
        )
        asset_repo.set_status(item.asset_id, asset_status.DESCRIBED)
        updated += 1

    if updated > 0:
        from sqlalchemy import text
        asset_ids = [item.asset_id for item in body.items]
        session.execute(
            text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = ANY(:ids)"),
            {"ids": asset_ids},
        )
        session.commit()

        lib_ids = {a.library_id for item in body.items if (a := asset_repo.get_by_id(item.asset_id))}
        lib_repo = LibraryRepository(session)
        for lid in lib_ids:
            lib_repo.bump_revision(lid)

    return {"updated": updated, "skipped": skipped}


class TranscriptSubmitRequest(BaseModel):
    srt: str
    language: str | None = None
    # "manual" for a person's transcript; a provider id (e.g. "whisper") for
    # machine output, which never replaces a manual one.
    source: str = "manual"


class TranscriptSubmitResponse(BaseModel):
    asset_id: str
    status: str


@router.post("/{asset_id}/transcript", response_model=TranscriptSubmitResponse)
def submit_transcript(
    asset_id: str,
    body: TranscriptSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TranscriptSubmitResponse:
    """Upload or replace an SRT transcript for a video asset."""
    from src.server.srt import parse_srt_to_text, validate_srt

    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Transcripts are only supported for video assets")

    # A person's transcript is human data: machine output never replaces it.
    if body.source != "manual" and asset.transcript_source == "manual":
        return TranscriptSubmitResponse(asset_id=asset_id, status="kept_manual")

    # Empty SRT = "checked, no speech" (e.g., silent video processed by Whisper)
    if not body.srt or not body.srt.strip():
        asset.transcript_srt = None
        asset.transcript_text = None
        asset.transcript_language = body.language
        asset.transcript_source = body.source
        asset.transcribed_at = utcnow()
        asset.has_transcript = False
        asset.updated_at = utcnow()
        session.add(asset)
        session.commit()
        return TranscriptSubmitResponse(asset_id=asset_id, status="no_speech")

    if not validate_srt(body.srt):
        raise HTTPException(status_code=400, detail="Invalid SRT format")

    plain_text = parse_srt_to_text(body.srt)

    asset.transcript_srt = body.srt
    asset.transcript_text = plain_text
    asset.transcript_language = body.language
    asset.transcript_source = body.source
    asset.transcribed_at = utcnow()
    asset.has_transcript = bool(plain_text.strip())
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search (works with or without vision metadata)
    from src.server.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    _tid = getattr(request.state, "tenant_id", None)
    if _tid:
        from src.server.search.sync import index_transcript_segments

        index_transcript_segments(_tid, asset, body.srt)

    LibraryRepository(session).bump_revision(asset.library_id)

    return TranscriptSubmitResponse(asset_id=asset_id, status="transcribed")


@router.delete("/{asset_id}/transcript", status_code=204)
def delete_transcript(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Remove a transcript from a video asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.transcript_srt = None
    asset.transcript_text = None
    asset.transcript_language = None
    asset.transcript_source = None
    asset.transcribed_at = None
    asset.has_transcript = False
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search
    from src.server.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    # Delete transcript segments from Quickwit
    _tid = getattr(request.state, "tenant_id", None)
    if _tid:
        try:
            from src.server.search.quickwit_client import QuickwitClient
            QuickwitClient().delete_tenant_transcript_documents(_tid, asset_id)
        except Exception as exc:
            logger.warning("Transcript segment delete failed for %s: %s", asset_id, exc)

    LibraryRepository(session).bump_revision(asset.library_id)


class NoteUpdateRequest(BaseModel):
    text: str


class NoteUpdateResponse(BaseModel):
    asset_id: str
    note: str | None
    note_author: str | None
    note_updated_at: str | None


@router.put("/{asset_id}/note", response_model=NoteUpdateResponse)
def update_note(
    asset_id: str,
    body: NoteUpdateRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> NoteUpdateResponse:
    """Add, update, or clear a freeform note on an asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    text = body.text.strip() if body.text else ""
    if text:
        # Resolve email for display
        email = getattr(request.state, "email", None) or user_id
        asset.note = text
        asset.note_author = email
        asset.note_updated_at = utcnow()
    else:
        asset.note = None
        asset.note_author = None
        asset.note_updated_at = None

    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    # Sync to search (works with or without vision metadata)
    from src.server.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return NoteUpdateResponse(
        asset_id=asset_id,
        note=asset.note,
        note_author=asset.note_author,
        note_updated_at=asset.note_updated_at.isoformat() if asset.note_updated_at else None,
    )


@router.delete("/{asset_id}/note", status_code=204)
def delete_note(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Delete a note from an asset."""
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    asset.note = None
    asset.note_author = None
    asset.note_updated_at = None
    asset.updated_at = utcnow()
    session.add(asset)
    session.commit()

    from src.server.search.sync import try_sync_asset
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)


class EmbeddingSubmitRequest(BaseModel):
    model_id: str
    model_version: str
    vector: list[float]


@router.post("/{asset_id}/embeddings", status_code=201)
def submit_embedding(
    asset_id: str,
    body: EmbeddingSubmitRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit an embedding vector for an asset."""
    from src.server.repository.tenant import AssetEmbeddingRepository
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    emb_repo = AssetEmbeddingRepository(session)
    emb_repo.upsert(
        asset_id=asset_id,
        model_id=body.model_id,
        model_version=body.model_version,
        vector=[float(x) for x in body.vector],
    )
    return {"ok": True}


class BatchEmbeddingItem(BaseModel):
    asset_id: str
    model_id: str
    model_version: str
    vector: list[float]


class BatchEmbeddingRequest(BaseModel):
    items: list[BatchEmbeddingItem]


@router.post("/batch-embeddings", status_code=200)
def submit_batch_embeddings(
    body: BatchEmbeddingRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit embedding vectors for multiple assets in one request."""
    from src.server.repository.tenant import AssetEmbeddingRepository
    asset_repo = AssetRepository(session)
    emb_repo = AssetEmbeddingRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        emb_repo.upsert(
            asset_id=item.asset_id,
            model_id=item.model_id,
            model_version=item.model_version,
            vector=[float(x) for x in item.vector],
        )
        updated += 1

    if updated > 0:
        session.commit()
        lib_ids = {a.library_id for item in body.items if (a := asset_repo.get_by_id(item.asset_id))}
        lib_repo = LibraryRepository(session)
        for lid in lib_ids:
            lib_repo.bump_revision(lid)

    return {"updated": updated, "skipped": skipped}


class BatchMoveItem(BaseModel):
    asset_id: str
    rel_path: str


class BatchMoveRequest(BaseModel):
    items: list[BatchMoveItem]


@router.post("/batch-moves", status_code=200)
def submit_batch_moves(
    body: BatchMoveRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Update rel_path for multiple assets (file moves detected by scan).
    409 moves_off when the account doesn't follow moves."""
    from src.server.tenant_settings import get_follow_moves

    if not get_follow_moves(session):
        raise DecisionRequiredError(
            "moves_off",
            "This account doesn't follow moves and renames: a file at a new path is a new asset. "
            "Turn follow_moves on (PATCH /v1/tenant/settings) to move assets.",
            {"follow_moves": False},
        )
    asset_repo = AssetRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        asset.rel_path = normalize_rel_path(item.rel_path)
        asset.search_synced_at = None
        asset.updated_at = utcnow()
        session.add(asset)
        updated += 1

    if updated > 0:
        session.commit()
        lib_ids = {a.library_id for item in body.items if (a := asset_repo.get_by_id(item.asset_id))}
        lib_repo = LibraryRepository(session)
        for lid in lib_ids:
            lib_repo.bump_revision(lid)

    return {"updated": updated, "skipped": skipped}


@router.get("/{asset_id}/preview")
def stream_or_enqueue_preview(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
):
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    _check_public_request(request, session, asset)

    if not asset.media_type.startswith("video"):
        raise HTTPException(status_code=422, detail="Preview only supported for video assets")

    storage = get_storage()

    if asset.video_preview_key:
        path = storage.abs_path(asset.video_preview_key)
        if path.exists():
            now = utcnow()
            last = asset.video_preview_last_accessed_at
            if last is None or (now - last).total_seconds() > 300:
                asset.video_preview_last_accessed_at = now
                session.add(asset)
                session.commit()
            from src.server.api.routers.playback import capped
            from src.server.tenant_settings import playback_cap

            path = capped(
                path, playback_cap(session, public=getattr(request.state, "is_public_request", False)),
                storage=storage, tenant_id=request.state.tenant_id, asset_id=asset_id, source="preview",
                version=f"{int(path.stat().st_mtime)}-{path.stat().st_size}",
                strip=getattr(request.state, "is_public_request", False),
            )
            return _stream_file_with_range(path, request, media_type="video/mp4")

        # File is missing on disk – clear key.
        asset.video_preview_key = None
        session.add(asset)
        session.commit()

    raise HTTPException(status_code=404, detail="No video preview available for this asset")


class ThumbnailKeyUpdateRequest(BaseModel):
    thumbnail_key: str


@router.post("/{asset_id}/thumbnail-key")
def set_thumbnail_key(
    asset_id: str,
    body: ThumbnailKeyUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Record a thumbnail_key for a video asset after the index worker extracts the first frame."""
    asset_repo = AssetRepository(session)
    asset_repo.update_thumbnail_key(asset_id, body.thumbnail_key)
    return {"asset_id": asset_id, "thumbnail_key": body.thumbnail_key}


@router.post("/upsert", response_model=UpsertAssetResponse)
def upsert_asset(
    body: UpsertAssetRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> UpsertAssetResponse:
    """
    Upsert by (library_id, rel_path): creates if not found; otherwise updates or skips.
    """
    lib_repo = LibraryRepository(session)
    library = lib_repo.get_by_id(body.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")

    file_mtime_dt: datetime | None = None
    if body.file_mtime:
        try:
            file_mtime_dt = datetime.fromisoformat(body.file_mtime.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid file_mtime format")

    asset_repo = AssetRepository(session)
    rel_path = normalize_rel_path(body.rel_path)
    existing = asset_repo.get_by_library_and_rel_path(body.library_id, rel_path)

    if existing is None:
        asset_repo.create_asset(
            library_id=body.library_id,
            rel_path=rel_path,
            file_size=body.file_size,
            file_mtime=file_mtime_dt,
            media_type=body.media_type,
        )
        return UpsertAssetResponse(action="added")

    if body.force or existing.file_size != body.file_size or existing.file_mtime != file_mtime_dt:
        existing.file_size = body.file_size
        existing.file_mtime = file_mtime_dt
        existing.availability = "online"
        session.add(existing)
        session.commit()
        return UpsertAssetResponse(action="updated")

    return UpsertAssetResponse(action="skipped")


# ---------------------------------------------------------------------------
# Face detection endpoints (ADR-009)
# ---------------------------------------------------------------------------


class FaceDetectionItem(BaseModel):
    """A single detected face submitted by any client.

    bounding_box: Normalized fractional coordinates (0.0–1.0).
        Canonical format: {x, y, w, h} — top-left origin, width/height.
        Also accepted: {x1, y1, x2, y2} — top-left and bottom-right corners.
        The server normalizes to {x, y, w, h} before storage.
    """
    bounding_box: dict[str, float]
    detection_confidence: float
    embedding: list[float] | None = None


def _normalize_bounding_box(bb: dict[str, float]) -> dict[str, float]:
    """Normalize bounding box to canonical {x, y, w, h} format.

    All clients MUST submit bounding boxes as normalized fractions (0.0–1.0)
    with top-left origin. The server stores and returns {x, y, w, h}.

    Accepted input formats:
      - {x, y, w, h}: position + size — used by Python CLI (InsightFace)
      - {x1, y1, x2, y2}: two corners — used by Swift client (Apple Vision)

    Output format (stored in DB, returned by API, used by web UI):
      - {x, y, w, h}: top-left corner + width/height, all 0.0–1.0
    """
    if "x" in bb:
        return bb
    # Convert x1,y1,x2,y2 → x,y,w,h
    return {
        "x": bb["x1"],
        "y": bb["y1"],
        "w": bb["x2"] - bb["x1"],
        "h": bb["y2"] - bb["y1"],
    }


class FaceSubmitRequest(BaseModel):
    detection_model: str = "insightface"
    detection_model_version: str = "buffalo_l"
    faces: list[FaceDetectionItem]


class FaceSubmitResponse(BaseModel):
    face_count: int
    face_ids: list[str]


class FaceListItem(BaseModel):
    face_id: str
    bounding_box: dict | None
    detection_confidence: float | None
    person: dict | None = None


class FaceListResponse(BaseModel):
    faces: list[FaceListItem]


def _generate_face_crops(
    tenant_id: str,
    asset: object,
    face_ids: list[str],
    faces_data: list[dict],
    session: object,
) -> None:
    """Generate 128x128 WebP face crop thumbnails from the asset proxy."""
    import io
    from PIL import Image

    from src.server.storage.local import get_storage

    storage = get_storage()
    proxy_key = asset.proxy_key  # type: ignore[union-attr]
    if not proxy_key:
        return

    proxy_path = storage.abs_path(proxy_key)
    if not proxy_path.exists():
        return

    try:
        img = Image.open(proxy_path).convert("RGB")
    except Exception:
        logger.warning("Cannot open proxy for face crops: %s", proxy_key)
        return

    try:
        w, h = img.size

        for face_id, face_data in zip(face_ids, faces_data):
            bb = face_data.get("bounding_box")
            if not bb:
                continue

            # Expand bounding box by 40% padding, clamp to image bounds
            pad = 0.4
            fx, fy, fw, fh = bb["x"], bb["y"], bb["w"], bb["h"]
            cx, cy = fx + fw / 2, fy + fh / 2
            side = max(fw, fh) * (1 + pad)
            x1 = max(0.0, cx - side / 2)
            y1 = max(0.0, cy - side / 2)
            x2 = min(1.0, cx + side / 2)
            y2 = min(1.0, cy + side / 2)

            # Convert fractions to pixels
            px1, py1 = int(x1 * w), int(y1 * h)
            px2, py2 = int(x2 * w), int(y2 * h)
            if px2 <= px1 or py2 <= py1:
                continue

            try:
                crop = img.crop((px1, py1, px2, py2))
                crop = crop.resize((128, 128), Image.LANCZOS)
                buf = io.BytesIO()
                crop.save(buf, format="WEBP", quality=80)
                crop_bytes = buf.getvalue()

                crop_key = storage.face_crop_key(tenant_id, asset.library_id, face_id)  # type: ignore[union-attr]
                storage.write(crop_key, crop_bytes)

                # Update face record
                from sqlalchemy import text as sa_text
                session.execute(  # type: ignore[union-attr]
                    sa_text("UPDATE faces SET crop_key = :key WHERE face_id = :fid"),
                    {"key": crop_key, "fid": face_id},
                )
            except Exception:
                logger.warning("Failed to generate crop for face %s", face_id, exc_info=True)

        session.commit()  # type: ignore[union-attr]
    finally:
        img.close()


class BatchFaceItem(BaseModel):
    asset_id: str
    detection_model: str = "insightface"
    detection_model_version: str = "buffalo_l"
    faces: list[FaceDetectionItem]


class BatchFaceRequest(BaseModel):
    items: list[BatchFaceItem]


@router.post("/batch-faces", status_code=200)
def submit_batch_faces(
    body: BatchFaceRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit face detections for multiple assets in one request."""
    from src.server.repository.tenant import FaceRepository

    asset_repo = AssetRepository(session)
    face_repo = FaceRepository(session)
    tenant_id = getattr(request.state, "tenant_id", None)
    processed = 0
    skipped = 0

    lib_ids: set[str] = set()
    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue

        faces_data = [
            {
                "bounding_box": _normalize_bounding_box(f.bounding_box),
                "detection_confidence": f.detection_confidence,
                "embedding": [float(x) for x in f.embedding] if f.embedding else None,
            }
            for f in item.faces
        ]
        face_ids = face_repo.submit_faces(
            asset_id=item.asset_id,
            detection_model=item.detection_model,
            detection_model_version=item.detection_model_version,
            faces=faces_data,
        )

        if tenant_id and asset.proxy_key:
            _generate_face_crops(tenant_id, asset, face_ids, faces_data, session)

        lib_ids.add(asset.library_id)
        processed += 1

    # Bump revision once per library
    lib_repo = LibraryRepository(session)
    for lid in lib_ids:
        lib_repo.bump_revision(lid)

    return {"processed": processed, "skipped": skipped}


@router.post("/{asset_id}/faces", response_model=FaceSubmitResponse, status_code=201)
def submit_faces(
    asset_id: str,
    body: FaceSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FaceSubmitResponse:
    """Submit face detections for an asset.

    Faces the new detections re-find keep their ids and confirmed assignments;
    confirmed faces that aren't re-found are kept. See FaceRepository.submit_faces.
    """
    from src.server.repository.tenant import FaceRepository

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    face_repo = FaceRepository(session)
    faces_data = [
        {
            "bounding_box": _normalize_bounding_box(f.bounding_box),
            "detection_confidence": f.detection_confidence,
            "embedding": [float(x) for x in f.embedding] if f.embedding else None,
        }
        for f in body.faces
    ]
    face_ids = face_repo.submit_faces(
        asset_id=asset_id,
        detection_model=body.detection_model,
        detection_model_version=body.detection_model_version,
        faces=faces_data,
    )

    # Generate face crop thumbnails
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset.proxy_key:
        _generate_face_crops(tenant_id, asset, face_ids, faces_data, session)

    # Bump library revision so UI reflects face_count changes
    LibraryRepository(session).bump_revision(asset.library_id)

    session.refresh(asset)
    return FaceSubmitResponse(face_count=asset.face_count or 0, face_ids=face_ids)


@router.get("/{asset_id}/faces", response_model=FaceListResponse, dependencies=[Depends(require_signed_in)])
def list_faces(
    asset_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FaceListResponse:
    """List all detected faces for an asset."""
    from src.server.repository.tenant import FaceRepository

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    face_repo = FaceRepository(session)
    faces = face_repo.get_by_asset_id(asset_id)
    face_ids = [f.face_id for f in faces]
    persons_by_face = face_repo.get_persons_for_faces(face_ids)

    return FaceListResponse(
        faces=[
            FaceListItem(
                face_id=f.face_id,
                bounding_box=f.bounding_box_json,
                detection_confidence=f.detection_confidence,
                person=(
                    {"person_id": p.person_id, "display_name": p.display_name, "dismissed": p.dismissed}
                    if (p := persons_by_face.get(f.face_id)) else None
                ),
            )
            for f in faces
        ]
    )
