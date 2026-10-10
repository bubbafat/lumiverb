"""Assets API: upsert for scanner, trash and restore. All routes require tenant auth (middleware)."""

import base64
import inspect
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, create_model, model_validator
from sqlmodel import Session

from src.shared.io_utils import normalize_rel_path
from src.server.api.dependencies import checked_rel_path, get_current_user_id, get_tenant_session, require_editor, require_signed_in
from src.server.api.dependencies import require_tenant_admin
from src.server.api.errors import ConflictError, DecisionRequiredError
from src.server.api.limits import MAX_IDS, MAX_PAGE
from src.shared import asset_status
from src.shared.io_utils import normalize_path_prefix
from src.server.repository.tenant import (
    MISSING_CONDITIONS,
    AssetMetadataRepository,
    AssetOcrRepository,
    AssetRepository,
    LibraryRepository,
)
from src.server.repository import lineage
from src.shared.producers import CLIP_MODEL_ID
from src.shared.producers import PERSON as P_PERSON
from src.server.api.routers.producers import LineageIn, require_lineage, with_source
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
    """One ffprobe pass over a video (see src/processing/video/probe.py).

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
    duration_sec: float | None = None
    ai_description: str | None = None
    ai_tags: list[str] = []
    ocr_text: str | None = None
    # Which of description, tags and OCR a person wrote (the one copy: no
    # machine copy is kept under it, Robert Oct 9).
    corrected: list[str] = []
    transcript_srt: str | None = None
    transcript_language: str | None = None
    transcribed_at: str | None = None
    # "manual" when a person's transcript is shown, else the machine that made it.
    transcript_source: str | None = None
    note: str | None = None
    note_author: str | None = None
    note_updated_at: str | None = None
    video_facet: VideoFacetModel | None = None
    # ADR-017: what the file doesn't say (a person's location, a guess or a
    # suggestion), beside the file's GPS above. Signed-in only.
    location: dict | None = None
    gps_accuracy_m: float | None = None


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
    asset_ids: list[str] = Field(max_length=MAX_IDS)
    # "user": a person trashed them; deleted for good after the trash days,
    # and scans leave them trashed. "missing": the scanner no longer finds the
    # files (archived; restored when they reappear). Required: a scanner and
    # a person mean different things.
    reason: Literal["user", "missing"]
    # A person's trash: required when any of the clips are in projects. In the
    # trash they're hidden there, and deleted for good they leave them.
    remove_from_projects: bool = False
    # "missing" for more than MASS_MISSING_MIN_FILES clips and more than half
    # of a library's clips in sight: the count of clips the 409 mass_missing
    # named, to say the files really are gone (not a half-mounted volume).
    confirm_missing: int | None = None


# Marking files missing in bulk looks exactly like a half-mounted volume:
# more than 50 clips and more than half of a library's clips in sight asks
# first (409 mass_missing). Half, not 5% (Robert, Oct 9): missing clips are
# archived, not deleted, and come back by themselves when the files do.
MASS_MISSING_MIN_FILES = 50
MASS_MISSING_MIN_FRACTION = 0.5


def _ask_about_mass_missing(session: Session, asset_ids: list[str], confirmed: int | None) -> None:
    """409 mass_missing when marking these missing would take more than
    MASS_MISSING_MIN_FILES clips and more than half of any library's clips in
    sight, unless confirmed is the number of clips it would take. Clients
    send a scan's missing clips in one request, so the rule sees them all."""
    from sqlalchemy import text as sa_text

    rows = session.execute(sa_text(
        """
        SELECT a.library_id,
               COUNT(*) FILTER (WHERE a.asset_id = ANY(:ids))::int AS going,
               COUNT(*)::int AS in_sight
        FROM assets a
        WHERE a.deleted_at IS NULL
          AND a.library_id IN (SELECT library_id FROM assets WHERE asset_id = ANY(:ids) AND deleted_at IS NULL)
        GROUP BY a.library_id
        """
    ), {"ids": asset_ids}).all()
    going = sum(r.going for r in rows)
    tripped = [r for r in rows
               if r.going > MASS_MISSING_MIN_FILES and r.going > MASS_MISSING_MIN_FRACTION * r.in_sight]
    if not tripped or confirmed == going:
        return
    worst = max(tripped, key=lambda r: r.going / r.in_sight)
    raise DecisionRequiredError(
        "mass_missing",
        f"{going} clips would be archived as missing, {round(100 * worst.going / worst.in_sight)}% of a "
        "library's clips: that usually means its storage isn't fully mounted. If the files really are gone, "
        f"send confirm_missing: {going}.",
        {"count": going, "libraries": [{"library_id": r.library_id, "missing": r.going, "in_sight": r.in_sight}
                                       for r in tripped]},
    )


class PickClipsRequest(BaseModel):
    """Clips by id, or every clip under a folder of a library (path "" is the
    whole library). The folder only picks the clips; it has no state of its own."""

    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
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
    asset_ids: list[str] = Field(max_length=MAX_IDS)


class RestoreResponse(BaseModel):
    restored: list[str]
    # Not in a person's trash (archived, missing, in sight, unknown), or their library is in the trash.
    skipped: list[str] = []
    # Of the restored, those archived before they were trashed: back in the archive, not in sight.
    to_archive: list[str] = []



class BatchTrashResponse(BaseModel):
    trashed: list[str]
    not_found: list[str]
    # Of the trashed, those that moved to an empty copy of their file instead
    # (copy, then delete the original): active again, at the copy's path.
    handed_over: list[str] = []


class VisionSubmitRequest(BaseModel):
    model_id: str
    model_version: str = "1"
    description: str
    tags: list[str] = []
    client_proxy_sha256: str | None = None
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class VisionSubmitResponse(BaseModel):
    asset_id: str
    status: str


def missing_filters(**flags: bool) -> frozenset[str]:
    """The missing_* filters asked for: one per producer the scheduler runs
    (its flag, src/producers/<artifact>/), and missing_face_embeddings."""
    return frozenset(flag for flag, on in flags.items() if on)


missing_filters.__signature__ = inspect.Signature([  # type: ignore[attr-defined]
    inspect.Parameter(flag, inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool)
    for flag in MISSING_CONDITIONS])


@router.get("/page", response_model=AssetPageResponse)
def page_assets(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    after: str | None = None,
    limit: int = Query(default=500, ge=1, le=MAX_PAGE),
    path_prefix: str | None = None,
    tag: str | None = None,
    missing: Annotated[frozenset[str], Depends(missing_filters)] = frozenset(),
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
    if getattr(request.state, "is_public_request", False):
        lib_repo = LibraryRepository(session)
        library = lib_repo.get_by_id(library_id)
        if library is None or not library.is_public:
            raise HTTPException(status_code=404, detail="Not found")
        # Ratings are a signed-in person's; who's in a photo isn't for visitors to probe,
        # nor where it was shot (repeated near searches would find it).
        if person_id or any(v is not None for v in (favorite, star_min, star_max, color, has_rating)):
            raise HTTPException(status_code=403, detail="That filter isn't available on public pages")
        if has_gps or near_lat is not None or near_lon is not None:
            raise HTTPException(status_code=403, detail="Location filters aren't available on public pages")
        camera = (camera_make, camera_model, lens_model, iso_min, iso_max, exposure_min_us, exposure_max_us,
                  aperture_min, aperture_max, focal_length_min, focal_length_max, has_exposure)
        if any(v is not None for v in camera):
            raise HTTPException(status_code=403, detail="Camera filters aren't available on public pages")
        from src.server.api.routers.query import PUBLIC_SORTS, require_public_folder

        if sort not in PUBLIC_SORTS:
            raise HTTPException(status_code=403, detail="That sort isn't available on public pages")
        if public_folder := normalize_path_prefix(path_prefix):
            require_public_folder(session, [library_id], public_folder)

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
        missing=missing,
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
    if getattr(request.state, "is_public_request", False):
        items = [_page_item_visitor_view(i) for i in items]
    next_cursor: str | None = None
    if items and len(items) == limit:
        last = assets[-1]
        sort_value = getattr(last, sort_col, None)
        if hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)

    return AssetPageResponse(items=items, next_cursor=next_cursor)


# Counts for a library: its clips, what scans left out (proxies, EXIF), each
# missing_* filter (one per producer the scheduler runs, and face
# embeddings), stale search entries, and failures waiting their turn (not in
# the counts above).
RepairSummary = create_model("RepairSummary", total_assets=(int, 0), missing_proxy=(int, 0), missing_exif=(int, 0),
                             **{flag: (int, 0) for flag in MISSING_CONDITIONS}, stale_search_sync=(int, 0),
                             waiting_failures=(int, 0))


def _waiting_failures_sql() -> str:
    """Clip-steps the missing_* counts leave out because their last try
    failed and their turn hasn't come."""
    from src.server.repository import lineage
    from src.shared.producers import MISSING_FLAGS

    # waiting() first: cheap, and false for nearly every clip, so the rest is rarely looked at.
    return " + ".join(f"COUNT(*) FILTER (WHERE {lineage.waiting(a)} AND {lineage.outstanding(a)})"
                      for a in MISSING_FLAGS.values())


@router.get("/repair-summary", response_model=RepairSummary, dependencies=[Depends(require_signed_in)])
def repair_summary(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
) -> RepairSummary:
    """Count assets missing various pipeline outputs for a library."""
    from sqlalchemy import text
    lib = LibraryRepository(session).get_by_id(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="Library not found")
    from src.server.search.sync import STALE_SEARCH  # the search sweep's own rule

    flags = list(MISSING_CONDITIONS)
    counts = ",\n".join(f"COUNT(*) FILTER (WHERE {MISSING_CONDITIONS[f]}) AS {f}" for f in flags)
    row = session.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE proxy_key IS NULL) AS missing_proxy,
                COUNT(*) FILTER (WHERE exif_extracted_at IS NULL AND media_type = 'image') AS missing_exif,
                {counts},
                COUNT(*) FILTER (WHERE {STALE_SEARCH}) AS stale_search_sync,
                {_waiting_failures_sql()} AS waiting_failures
            FROM active_assets a
            {lineage.LINEAGE_JOIN}
            WHERE library_id = :library_id
        """),
        {"library_id": library_id},
    ).mappings().one()
    return RepairSummary(total_assets=row["total"], missing_proxy=row["missing_proxy"],
                         missing_exif=row["missing_exif"], **{f: row[f] for f in flags},
                         waiting_failures=row["waiting_failures"], stale_search_sync=row["stale_search_sync"])


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
                  q.get("public_project_id"))


def _fill_described(session: Session, asset_id: str, response: AssetResponse) -> None:
    """The description, tags and OCR a clip shows (what a person wrote, else
    the machine's), and which a person wrote."""
    from src.server.repository.corrections import CorrectionsRepository, apply
    from src.server.repository.corrections import machine_tags as tags_in

    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    data = (meta.data if meta else None) or {}
    description, tags, ocr_text, corrected = apply(
        CorrectionsRepository(session).get(asset_id), data.get("description") or None, tags_in(data),
        AssetOcrRepository(session).text_for(asset_id) or None)
    response.ai_description = description or None
    response.ai_tags = tags
    response.ocr_text = ocr_text or None
    response.corrected = corrected


def _asset_detail(session: Session, request: Request, asset: Asset) -> AssetResponse:
    """A clip's whole detail, as GET /v1/assets/{id} returns it (trimmed for a public page)."""
    response = _to_asset_response(asset)
    _fill_described(session, asset.asset_id, response)
    facet = AssetRepository(session).get_video_facet(asset.asset_id)
    # Stored rows are returned as they are (validation is for writes).
    response.video_facet = VideoFacetModel.model_construct(**facet) if facet else None
    _trim_public_transcript(request, session, response)
    if not getattr(request.state, "is_public_request", False):
        from src.server.repository.locations import for_asset

        response.location = for_asset(session, asset.asset_id)
    return response


def _project_visitor_view(response: AssetResponse) -> AssetResponse:
    """What a public page (a project's or a library's) may show of a clip: what its clip list gives
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


def _page_item_visitor_view(item: AssetPageItem) -> AssetPageItem:
    """A page item as a visitor to a public library sees it: what
    _project_visitor_view keeps (shape, time, length), not where it lives,
    where it was shot or what shot it."""
    return AssetPageItem(
        asset_id=item.asset_id,
        rel_path="",
        file_size=0,
        file_mtime=None,
        sha256=None,
        media_type=item.media_type,
        width=item.width,
        height=item.height,
        taken_at=item.taken_at,
        status=item.status,
        duration_sec=item.duration_sec,
        has_analysis_proxy=item.has_analysis_proxy,
    )


def _trim_public_transcript(request: Request, session: Session, response: AssetResponse) -> None:
    """A public page's transcript stops where its playback does, and it shows no notes:
    they're the team's working notes (Robert's call, Oct 8). Nor does it say
    what a person wrote: a visitor sees the result."""
    if not getattr(request.state, "is_public_request", False):
        return
    response.note = None
    response.note_author = None
    response.note_updated_at = None
    response.corrected = []
    response.transcript_source = None
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
        gps_accuracy_m=asset.gps_accuracy_m,
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
        duration_sec=asset.duration_sec,
        transcript_srt=asset.transcript_srt,
        transcript_language=asset.transcript_language,
        transcript_source=asset.transcript_source,
        transcribed_at=asset.transcribed_at.isoformat() if asset.transcribed_at else None,
        note=asset.note,
        note_author=asset.note_author,
        note_updated_at=asset.note_updated_at.isoformat() if asset.note_updated_at else None,
    )


@router.get("/by-path", response_model=AssetResponse)
def get_asset_by_path(
    library_id: str,
    rel_path: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> AssetResponse:
    """Return a single asset by library_id + rel_path. 404 if not found or trashed."""
    if getattr(request.state, "is_public_request", False):
        raise HTTPException(status_code=403, detail="Not available on public pages")  # a file-name probe
    rel_path = normalize_rel_path(rel_path)
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_library_and_rel_path(library_id, rel_path)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail=f"Asset not found: {rel_path}")
    return _asset_detail(session, request, asset)


class ProjectUsageRequest(BaseModel):
    asset_ids: list[str] = Field(default_factory=list, max_length=MAX_IDS)
    # Every clip in these libraries, e.g. before emptying the library trash.
    library_ids: list[str] = Field(default_factory=list, max_length=MAX_IDS)


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
    search already leaves out clips out of sight; this keeps pages full.
    Only those still out of sight then: one back meanwhile (handed over to
    its copy by another request, say) keeps its documents, transcript
    segments too, which no sweep puts back."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset_ids:
        background.add_task(_drop_from_search, tenant_id, list(asset_ids))


def _drop_from_search(tenant_id: str, asset_ids: list[str]) -> None:
    from sqlalchemy import text as sa_text

    from src.server.database import get_tenant_session
    from src.server.search.quickwit_client import QuickwitClient

    try:
        with get_tenant_session(tenant_id) as session:
            in_sight = set(session.execute(
                sa_text("SELECT asset_id FROM assets WHERE asset_id = ANY(:ids) AND deleted_at IS NULL"),
                {"ids": asset_ids},
            ).scalars())
    except Exception as exc:  # noqa: BLE001 — best effort, as the delete itself
        logger.warning("Couldn't check which clips came back before dropping them from search: %s", exc)
        return
    gone = [a for a in asset_ids if a not in in_sight]
    if gone:
        QuickwitClient().delete_tenant_documents_by_asset_ids(tenant_id, gone)


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
                index_transcript_segments(session, tenant_id, asset)

        background.add_task(run)


def _refresh_grids(session: Session, asset_ids: list[str]) -> None:
    """Bump the revision of every library these clips are in, so open grids and folder trees reload."""
    asset_repo, library_repo = AssetRepository(session), LibraryRepository(session)
    for library_id in asset_repo.library_ids_of(asset_ids):
        library_repo.bump_revision(library_id)


@router.delete("", response_model=BatchTrashResponse, dependencies=[Depends(require_editor)])
def batch_trash_assets(
    body: BatchTrashRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> BatchTrashResponse:
    """Take clips out of sight: a person's trash ("user"), or files a scan no longer finds ("missing").

    Needs an editor. A person's trash takes archived clips too (deleting an
    archived clip moves it to the trash), and asks first about clips that
    projects use (409 in_projects). Marking files missing asks first when it
    would take most of a library (409 mass_missing), and hands each over to
    an empty copy of its file when there is one (follow moves).
    """
    reason = body.reason
    asset_repo = AssetRepository(session)
    if reason == "user":
        if not body.remove_from_projects:
            # Only about clips this would trash: one already in the trash needs no answer.
            _ask_about_projects(session, request, asset_repo.trashable(body.asset_ids))
    else:
        _ask_about_mass_missing(session, body.asset_ids, body.confirm_missing)
    trashed_ids, not_found_ids = asset_repo.trash_many(body.asset_ids, reason=reason)
    handed_over: list[str] = []
    if trashed_ids and reason == "missing":
        # Copy, then delete the original: the asset moves to its empty copy.
        from src.server.api.routers.trash import hand_over_to_copies
        from src.server.tenant_settings import get_follow_moves

        if get_follow_moves(session):
            handed_over = hand_over_to_copies(session, request, trashed_ids)
    # Not those that moved: they're back in search, transcripts and all.
    _out_of_search(background, request, [a for a in trashed_ids if a not in handed_over])
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
    response = _asset_detail(session, request, asset)
    # A visitor, by a public project or a public library, sees the same trimmed clip.
    return _project_visitor_view(response) if getattr(request.state, "is_public_request", False) else response


class VideoFacetSubmit(VideoFacetModel):
    """A probe result, with how it was made."""

    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.put("/{asset_id}/video-facet", response_model=VideoFacetModel, dependencies=[Depends(require_editor)])
def put_video_facet(
    asset_id: str,
    body: VideoFacetSubmit,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> VideoFacetModel:
    """Store a video's probe result (replaces any earlier one). Sets duration_sec."""
    made = require_lineage(body.lineage, "probe")  # before anything is saved
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Video facets are only for video assets")
    facet = body.model_dump(exclude={"lineage"})
    asset_repo.upsert_video_facet(asset_id, facet)
    lineage.record(session, asset_id, "probe", made)
    LibraryRepository(session).bump_revision(asset.library_id)
    return VideoFacetModel(**facet)


class CaptureSubmit(BaseModel):
    """A clip's capture facts read from its file (the capture producer, ADR-017)."""

    taken_at: datetime | None = None  # the camera's wall clock, as UTC; None keeps the stored one
    taken_at_offset_min: int | None = Field(default=None, ge=-14 * 60, le=14 * 60)
    gps_accuracy_m: float | None = Field(default=None, ge=0, lt=1e7, allow_inf_nan=False)
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class CaptureResponse(BaseModel):
    changed: bool  # taken_at or its zone differ from what was stored


@router.put("/{asset_id}/capture", response_model=CaptureResponse, dependencies=[Depends(require_tenant_admin)])
def put_capture(
    asset_id: str,
    body: CaptureSubmit,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> CaptureResponse:
    """Store a clip's capture facts, read again from its file (the scheduler's
    capture producer, so admins: its key is an admin's): when it was
    taken, its zone, and its GPS accuracy (only with the file's GPS). The
    file's GPS itself is the scan's and isn't touched."""
    made = require_lineage(body.lineage, "capture")  # before anything is saved
    taken_at = body.taken_at
    if taken_at is not None and taken_at.tzinfo is None:
        from datetime import timezone

        taken_at = taken_at.replace(tzinfo=timezone.utc)
    stored = AssetRepository(session).set_capture(asset_id, taken_at, body.taken_at_offset_min, body.gps_accuracy_m)
    if stored is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    before, after = stored
    lineage.record(session, asset_id, "capture", made, commit=False)
    session.commit()
    return CaptureResponse(changed=(before["taken_at"], before["taken_at_offset_min"])
                           != (after["taken_at"], after["taken_at_offset_min"]))


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
    asset_repo = AssetRepository(session)
    if not asset_repo.trashable([asset_id]):
        raise HTTPException(status_code=404, detail="Asset not found or already trashed")
    if not remove_from_projects:
        _ask_about_projects(session, request, [asset_id])
    if not asset_repo.trash(asset_id, reason="user"):
        raise HTTPException(status_code=404, detail="Asset not found or already trashed")
    _out_of_search(background, request, [asset_id])
    _refresh_grids(session, [asset_id])


def reindex_restored_asset(request: Request, session: Session, asset: Asset) -> None:
    """Put a restored asset's transcript segments back in search; the sync
    sweep re-indexes the asset and its scenes (restore queued them)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id and asset.transcript_srt:
        from src.server.search.sync import index_transcript_segments

        index_transcript_segments(session, tenant_id, asset)


_NOT_THE_TRASH = {
    "archived": ("archived", "This clip is archived, not in the trash: unarchive it (POST /v1/assets/unarchive)."),
    "missing": ("file_missing", "This clip's file is missing; it comes back when the file does."),
    "library_trashed": ("library_trashed", "This clip went to the trash with its library; restore the library."),
    "path_taken": ("path_taken", "Another clip is at this clip's path now (a different file there is its own clip);"
                                 " this one stays in the trash."),
}


@router.post("/{asset_id}/restore", status_code=204)
def restore_asset(
    asset_id: str,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> None:
    """Take one clip out of the trash, back to where it was (in sight, or the
    archive if it was archived before). 404 if not found or not deleted;
    409 archived, file_missing or library_trashed for a clip that isn't in a
    person's trash."""
    asset_repo = AssetRepository(session)
    outcome = asset_repo.restore(asset_id)
    if outcome in _NOT_THE_TRASH:
        raise ConflictError(*_NOT_THE_TRASH[outcome])
    if outcome != "restored":
        raise HTTPException(status_code=404, detail="Asset not found or not trashed")
    back = session.get(Asset, asset_id)
    if back is not None and back.deleted_at is None:  # else it went back to the archive
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
    """Take clips out of the trash, back to where they were: in sight, or the
    archive for those archived before they were trashed (to_archive).
    Anything else asked for is skipped: archived or missing clips, clips in
    sight, and clips whose library is in the trash."""
    restored, skipped, to_archive = AssetRepository(session).restore_many(body.asset_ids)
    _back_in_search(background, request, session, [a for a in restored if a not in set(to_archive)])
    _refresh_grids(session, restored)
    return RestoreResponse(restored=restored, skipped=skipped, to_archive=to_archive)


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


@router.post("/{asset_id}/vision", response_model=VisionSubmitResponse, dependencies=[Depends(require_editor)])
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
    made = require_lineage(body.lineage, "vision")  # before anything is saved
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")

    if body.client_proxy_sha256 is not None and asset.proxy_sha256 is not None:
        if body.client_proxy_sha256 != asset.proxy_sha256:
            raise ConflictError(
                "proxy_hash_mismatch",
                "Client proxy does not match server proxy. Re-download the proxy and retry.",
            )

    AssetMetadataRepository(session).upsert(
        asset_id=asset_id,
        model_id=body.model_id,
        model_version=body.model_version,
        data={"description": body.description, "tags": body.tags},
    )
    AssetRepository(session).set_status(asset_id, asset_status.DESCRIBED)
    lineage.record(session, asset_id, "vision", made)

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
    # The vision model that read it.
    model_id: str = Field(default="", max_length=200)
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/{asset_id}/ocr", status_code=200, dependencies=[Depends(require_editor)])
def submit_ocr(
    asset_id: str,
    body: OcrSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit the text read in an asset's image ("" when there's none). Kept
    apart from its description: no description needed, and describing it
    again leaves this alone."""
    made = require_lineage(body.lineage, "ocr")  # before anything is saved
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")

    AssetOcrRepository(session).upsert(asset_id, body.ocr_text, body.model_id, commit=False)
    lineage.record(session, asset_id, "ocr", made, outcome="ok" if body.ocr_text else "empty",
                   commit=False)
    session.commit()  # the text and how it was made, together

    # Re-sync search
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)
    from src.server.search.sync import try_sync_asset
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))

    LibraryRepository(session).bump_revision(asset.library_id)

    return {"asset_id": asset_id, "ocr_text": body.ocr_text}


class BatchOcrItem(BaseModel):
    asset_id: str
    ocr_text: str
    source_sha256: str | None = None  # the file it was made from; the batch's lineage otherwise


class BatchOcrRequest(BaseModel):
    items: list[BatchOcrItem]
    # The vision model that read them.
    model_id: str = Field(default="", max_length=200)
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/batch-ocr", status_code=200, dependencies=[Depends(require_editor)])
def submit_batch_ocr(
    body: BatchOcrRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit OCR text for multiple assets in one request. Assets that don't
    exist (or are in the trash) are skipped."""
    made = require_lineage(body.lineage, "ocr")  # before anything is saved
    ocr_repo = AssetOcrRepository(session)
    asset_repo = AssetRepository(session)
    updated = 0
    skipped = 0

    for item in body.items:
        asset = asset_repo.get_by_id(item.asset_id)
        if asset is None or asset.deleted_at is not None:
            skipped += 1
            continue
        ocr_repo.upsert(item.asset_id, item.ocr_text, body.model_id, commit=False)
        lineage.record(session, item.asset_id, "ocr", with_source(made, item.source_sha256),
                       outcome="ok" if item.ocr_text else "empty", commit=False)
        updated += 1
    session.commit()

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
    source_sha256: str | None = None  # the file it was made from; the batch's lineage otherwise


class BatchVisionRequest(BaseModel):
    items: list[BatchVisionItem]
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/batch-vision", status_code=200, dependencies=[Depends(require_editor)])
def submit_batch_vision(
    body: BatchVisionRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit AI vision results for multiple assets in one request."""
    made = require_lineage(body.lineage, "vision")  # before anything is saved
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
        lineage.record(session, item.asset_id, "vision", with_source(made, item.source_sha256))
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


class CorrectionsRequest(BaseModel):
    """Only the fields sent change. A string (or `tags`, the whole list) is
    what the person wrote: the one copy, which the machine never replaces
    (its value is dropped; a tag list stays as written). null removes what
    they wrote: none is left, and the machine makes it again."""

    description: str | None = Field(default=None, max_length=10_000)
    ocr_text: str | None = Field(default=None, max_length=20_000)
    tags: list[Annotated[str, Field(max_length=100)]] | None = Field(default=None, max_length=200)


@router.patch("/{asset_id}/corrections", response_model=AssetResponse, dependencies=[Depends(require_editor)])
def correct_asset(
    asset_id: str,
    body: CorrectionsRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AssetResponse:
    """A person writes a clip's description, OCR or tags: the one copy
    (Robert, Oct 9: edits keep only the latest). Describing or reading the
    clip again never replaces it. Returns the clip's detail."""
    from src.server.repository.corrections import CorrectionsRepository

    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    CorrectionsRepository(session).update(asset_id, body.model_dump(include=body.model_fields_set), user_id)
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset_id)

    from src.server.search.sync import try_sync_asset

    # Stale until search has it: undoing every correction leaves no row
    # whose time says so, and search may be down.
    asset.search_synced_at = None
    session.add(asset)
    session.commit()
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))
    LibraryRepository(session).bump_revision(asset.library_id)

    return _asset_detail(session, request, asset)


class TranscriptSubmitRequest(BaseModel):
    srt: str
    language: str | None = None
    # Whose it is, which every upload says: "manual" for a person's; a provider
    # id (e.g. "whisper") for machine output, which never replaces a person's.
    source: str = Field(min_length=1, max_length=64)
    # How a machine transcript was made (a person's needs none).
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


class TranscriptSubmitResponse(BaseModel):
    asset_id: str
    status: str


def _transcript_lineage(body: TranscriptSubmitRequest) -> dict:
    """record() arguments: a person's transcript is a person's (current,
    never made again over); a machine's must say how it was made (422
    lineage_required, checked before anything is saved)."""
    if body.source == "manual":
        return {"lineage": None, "person": True}
    return {"lineage": require_lineage(body.lineage, "transcript")}


def _lock_transcript(session: Session, asset: Asset) -> None:
    """One write of a clip's transcript at a time (a person's and the
    machine's never cross), with the clip as it is now."""
    from sqlalchemy import text as sa_text

    session.execute(sa_text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset.asset_id})
    session.refresh(asset)


def _persons_transcript(session: Session, asset: Asset) -> bool:
    """The clip's transcript is a person's call: theirs is shown, or they
    removed the machine's for good (none shown, a person's lineage)."""
    from sqlalchemy import text as sa_text

    if asset.transcript_source == "manual":
        return True
    if _shown_transcript(asset) is not None:
        return False
    return session.execute(sa_text(
        "SELECT producer FROM artifact_lineage WHERE asset_id = :a AND artifact = 'transcript'"
    ), {"a": asset.asset_id}).scalar() == P_PERSON


def _shown_transcript(asset: Asset) -> str | None:
    """Whose transcript a clip shows: "manual" (a person's), "machine", or None."""
    if asset.transcript_source == "manual":
        return "manual"
    if asset.transcript_source or asset.transcript_srt or asset.transcribed_at:
        return "machine"
    return None


@router.post("/{asset_id}/transcript", response_model=TranscriptSubmitResponse, dependencies=[Depends(require_editor)])
def submit_transcript(
    asset_id: str,
    body: TranscriptSubmitRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> TranscriptSubmitResponse:
    """Upload or replace a video's SRT transcript. A clip has one: the
    latest (Robert, Oct 9). A person's ("manual", editors) replaces whatever
    is there; a machine's never replaces a person's (status kept_manual, and
    nothing of it is kept)."""
    from src.server.srt import parse_srt_to_text, validate_srt

    made = _transcript_lineage(body)  # a machine's says how it was made, before anything is saved
    asset_repo = AssetRepository(session)
    asset = asset_repo.get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.media_type != "video":
        raise HTTPException(status_code=400, detail="Transcripts are only supported for video assets")
    _lock_transcript(session, asset)
    if body.source != "manual" and _persons_transcript(session, asset):
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
        lineage.record(session, asset_id, "transcript", **made, outcome="empty")
        _transcript_searched(session, request, asset)
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
    lineage.record(session, asset_id, "transcript", **made, outcome="ok" if asset.has_transcript else "empty")
    _transcript_searched(session, request, asset)
    return TranscriptSubmitResponse(asset_id=asset_id, status="transcribed")


def _transcript_searched(session: Session, request: Request, asset: Asset) -> None:
    """Search has the clip's transcript as it is now (segments replaced), and its library changed."""
    from src.server.search.sync import index_transcript_segments, try_sync_asset

    # Stale until search has it (search may be down: the sweep catches up).
    asset.search_synced_at = None
    session.add(asset)
    session.commit()
    meta = AssetMetadataRepository(session).get_latest(asset_id=asset.asset_id)
    try_sync_asset(session, asset, meta, tenant_id=getattr(request.state, "tenant_id", None))
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        try:
            index_transcript_segments(session, tid, asset)  # the transcript as committed, or none
        except Exception as exc:  # noqa: BLE001 — search catches up on its own
            logger.warning("Transcript segment re-index failed for %s: %s", asset.asset_id, exc)
    LibraryRepository(session).bump_revision(asset.library_id)


@router.delete("/{asset_id}/transcript", status_code=204, dependencies=[Depends(require_editor)])
def delete_transcript(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    which: Annotated[Literal["manual", "machine"] | None, Query()] = None,
) -> None:
    """Remove the transcript a video shows (it has one: the latest).

    A person's: none is left, and the machine makes one again (it's missing
    again). The machine's: gone for good, a person's choice that nothing
    makes again. `which` (optional) says whose the caller means: when the
    clip shows the other one, 409 transcript_changed (a second click never
    removes both). When it shows none, there's nothing to do."""
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    _lock_transcript(session, asset)

    shown = _shown_transcript(asset)
    if shown is None:
        return
    if which is not None and shown != which:
        raise ConflictError(
            "transcript_changed",
            f"The clip now shows {'a person' if shown == 'manual' else 'the machine'}'s transcript, "
            f"not {'a person' if which == 'manual' else 'the machine'}'s. Nothing was removed.",
            {"shown": shown},
        )

    asset.transcript_srt = None
    asset.transcript_text = None
    asset.transcript_language = None
    asset.transcript_source = None
    asset.transcribed_at = None
    asset.updated_at = utcnow()
    if shown == "manual":
        asset.has_transcript = None  # missing again: the machine makes one
        session.add(asset)
        lineage.forget(session, [asset_id], "transcript", commit=False)
    else:
        asset.has_transcript = False
        session.add(asset)
        # A person removed the machine's: their choice stands, nothing makes it again.
        lineage.record(session, asset_id, "transcript", None, person=True, outcome="empty", commit=False)
    session.commit()
    _transcript_searched(session, request, asset)


class NoteUpdateRequest(BaseModel):
    text: str


class NoteUpdateResponse(BaseModel):
    asset_id: str
    note: str | None
    note_author: str | None
    note_updated_at: str | None


@router.put("/{asset_id}/note", response_model=NoteUpdateResponse, dependencies=[Depends(require_editor)])
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


@router.delete("/{asset_id}/note", status_code=204, dependencies=[Depends(require_editor)])
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
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/{asset_id}/embeddings", status_code=201, dependencies=[Depends(require_editor)])
def submit_embedding(
    asset_id: str,
    body: EmbeddingSubmitRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit an embedding vector for an asset. CLIP's (the producer's
    artifact) must say how it was made (lineage)."""
    from src.server.repository.tenant import AssetEmbeddingRepository
    made = require_lineage(body.lineage, "clip") if body.model_id == CLIP_MODEL_ID else None
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
    if body.model_id == CLIP_MODEL_ID:  # another model's vectors aren't the CLIP producer's artifact
        lineage.record(session, asset_id, "clip", made)
    return {"ok": True}


class BatchEmbeddingItem(BaseModel):
    asset_id: str
    model_id: str
    model_version: str
    vector: list[float]
    source_sha256: str | None = None  # the file it was made from; the batch's lineage otherwise


class BatchEmbeddingRequest(BaseModel):
    items: list[BatchEmbeddingItem]
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/batch-embeddings", status_code=200, dependencies=[Depends(require_editor)])
def submit_batch_embeddings(
    body: BatchEmbeddingRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit embedding vectors for multiple assets in one request. CLIP's
    (the producer's artifact) must say how they were made (lineage)."""
    from src.server.repository.tenant import AssetEmbeddingRepository
    made = require_lineage(body.lineage, "clip") if any(i.model_id == CLIP_MODEL_ID for i in body.items) else None
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
        if item.model_id == CLIP_MODEL_ID:
            lineage.record(session, item.asset_id, "clip", with_source(made, item.source_sha256),
                           commit=False)
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


@router.post("/batch-moves", status_code=200, dependencies=[Depends(require_editor)])
def submit_batch_moves(
    body: BatchMoveRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Update rel_path for multiple assets (file moves detected by scan).
    409 moves_off when the account doesn't follow moves."""
    from src.server.tenant_settings import get_follow_moves

    # Every path checked before any move is made.
    moved_to = {item.asset_id: checked_rel_path(item.rel_path) for item in body.items}
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
        asset.rel_path = moved_to[item.asset_id]
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


@router.post("/upsert", response_model=UpsertAssetResponse, dependencies=[Depends(require_editor)])
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
    rel_path = checked_rel_path(body.rel_path)
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
    # The model that embedded the faces (the faces producer's `model`).
    embedding_model: str = Field(default="buffalo_l", max_length=100)
    faces: list[FaceDetectionItem]
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


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
    # The model that embedded the faces (the faces producer's `model`).
    embedding_model: str = Field(default="buffalo_l", max_length=100)
    faces: list[FaceDetectionItem]
    source_sha256: str | None = None  # the file it was made from; the batch's lineage otherwise


class BatchFaceRequest(BaseModel):
    items: list[BatchFaceItem]
    lineage: Any = None  # how it was made (LineageIn): require_lineage judges it


@router.post("/batch-faces", status_code=200, dependencies=[Depends(require_editor)])
def submit_batch_faces(
    body: BatchFaceRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Submit face detections for multiple assets in one request."""
    made = require_lineage(body.lineage, "faces")  # before anything is saved
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
            embedding_model=item.embedding_model,
        )
        lineage.record(session, item.asset_id, "faces", with_source(made, item.source_sha256),
                       outcome="ok" if faces_data else "empty")

        if tenant_id and asset.proxy_key:
            _generate_face_crops(tenant_id, asset, face_ids, faces_data, session)

        lib_ids.add(asset.library_id)
        processed += 1

    # Bump revision once per library
    lib_repo = LibraryRepository(session)
    for lid in lib_ids:
        lib_repo.bump_revision(lid)

    return {"processed": processed, "skipped": skipped}


@router.post("/{asset_id}/faces", response_model=FaceSubmitResponse, status_code=201, dependencies=[Depends(require_editor)])
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
    made = require_lineage(body.lineage, "faces")  # before anything is saved
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
        embedding_model=body.embedding_model,
    )
    lineage.record(session, asset_id, "faces", made, outcome="ok" if faces_data else "empty")

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
