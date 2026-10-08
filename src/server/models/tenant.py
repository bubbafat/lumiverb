"""Tenant database models: libraries, assets, scenes, metadata, workers, Phase 2 stubs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.shared.utils import utcnow


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------


class Library(SQLModel, table=True):
    __tablename__ = "libraries"

    library_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    root_path: str = Field(nullable=False)
    status: str = Field(default="active", nullable=False)
    last_scan_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    is_public: bool = Field(default=False, nullable=False)
    revision: int = Field(default=0, nullable=False)
    cover_asset_id: str | None = Field(
        default=None, foreign_key="assets.asset_id", nullable=True
    )
    # When it went in the trash (status "trashed"); its trash days count from here.
    trashed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class LibraryChange(SQLModel, table=True):
    """A path the storage's own machine saw change, waiting to be scanned."""

    __tablename__ = "library_changes"
    __table_args__ = (UniqueConstraint("library_id", "rel_path", name="uq_library_changes_library_path"),)

    change_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False, index=True)
    rel_path: str = Field(nullable=False)
    reported_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    version: int = Field(sa_column=Column(BigInteger, nullable=False))


class LibraryPathFilter(SQLModel, table=True):
    __tablename__ = "library_path_filters"

    filter_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False)
    type: str = Field(nullable=False)  # "include" | "exclude"
    pattern: str = Field(nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class TenantPathFilterDefault(SQLModel, table=True):
    __tablename__ = "tenant_path_filter_defaults"

    default_id: str = Field(primary_key=True)
    tenant_id: str = Field(nullable=False)
    type: str = Field(nullable=False)  # "include" | "exclude"
    pattern: str = Field(nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Asset(SQLModel, table=True):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("library_id", "rel_path", name="uq_assets_library_rel_path"),
        CheckConstraint("media_type IN ('image', 'video')", name="ck_assets_media_type"),
    )

    asset_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False)
    rel_path: str = Field(nullable=False)
    sha256: str | None = Field(default=None, nullable=True)
    file_size: int = Field(sa_column=Column(BigInteger, nullable=False))
    file_mtime: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    media_type: str = Field(nullable=False)
    width: int | None = Field(default=None, nullable=True)
    height: int | None = Field(default=None, nullable=True)
    duration_sec: float | None = Field(default=None, nullable=True)
    proxy_key: str | None = Field(default=None, nullable=True)
    proxy_sha256: str | None = Field(default=None, nullable=True)
    thumbnail_key: str | None = Field(default=None, nullable=True)
    thumbnail_sha256: str | None = Field(default=None, nullable=True)
    exif: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    exif_extracted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    camera_make: str | None = Field(default=None, nullable=True)
    camera_model: str | None = Field(default=None, nullable=True)
    taken_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    gps_lat: float | None = Field(default=None, nullable=True)
    gps_lon: float | None = Field(default=None, nullable=True)
    iso: int | None = Field(default=None, nullable=True)
    exposure_time_us: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    aperture: float | None = Field(default=None, nullable=True)
    focal_length: float | None = Field(default=None, nullable=True)
    focal_length_35mm: float | None = Field(default=None, nullable=True)
    lens_model: str | None = Field(default=None, nullable=True)
    flash_fired: bool | None = Field(default=None, nullable=True)
    orientation: int | None = Field(default=None, nullable=True)
    availability: str = Field(default="online", nullable=False)
    status: str = Field(default="pending", nullable=False)
    video_indexed: bool = Field(default=False, nullable=False)
    video_preview_key: str | None = Field(default=None, nullable=True)
    video_preview_last_accessed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    video_preview_generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    # Full-length low-resolution copy with audio, for analysis only.
    analysis_proxy_key: str | None = Field(default=None, nullable=True)
    analysis_proxy_sha256: str | None = Field(default=None, nullable=True)
    analysis_proxy_generated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    error_message: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    deleted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    # Why the clip is out of sight (deleted_at is when it got there).
    # Archived, kept forever:
    #   "archived" = a person archived it; scans leave it archived.
    #   "missing"  = a scan no longer finds the file; restored when it reappears.
    # In the trash, deleted for good after the account's trash days:
    #   "user"     = a person trashed it; scans leave it trashed.
    #   "library"  = it went with its library, and comes back with it.
    # "handed_over" lives only inside a copy-then-delete handover.
    deleted_reason: str | None = Field(default=None, nullable=True)
    # In a person's trash ("user"): what it was before, "archived" or
    # "missing", so restoring puts it back in the archive; None = in sight.
    trashed_from: str | None = Field(default=None, nullable=True)
    search_synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    face_count: int | None = Field(default=None, nullable=True)
    has_transcript: bool | None = Field(default=None, nullable=True)
    transcript_srt: str | None = Field(default=None, nullable=True)
    transcript_text: str | None = Field(default=None, nullable=True)
    transcript_language: str | None = Field(default=None, nullable=True)
    # "manual" or a provider id (e.g. "whisper"). Machine output never
    # replaces a manual transcript.
    transcript_source: str | None = Field(default=None, nullable=True)
    transcribed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    note: str | None = Field(default=None, nullable=True)
    note_author: str | None = Field(default=None, nullable=True)
    note_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class VideoScene(SQLModel, table=True):
    __tablename__ = "video_scenes"

    scene_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    scene_index: int = Field(nullable=False)
    start_ms: int = Field(nullable=False)
    end_ms: int = Field(nullable=False)
    rep_frame_ms: int = Field(nullable=False)
    rep_frame_sha256: str | None = Field(default=None, nullable=True)
    proxy_key: str | None = Field(default=None, nullable=True)
    thumbnail_key: str | None = Field(default=None, nullable=True)
    description: str | None = Field(default=None, nullable=True)
    tags: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    sharpness_score: float | None = Field(default=None, nullable=True)
    keep_reason: str | None = Field(default=None, nullable=True)
    phash: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    search_synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class VideoIndexChunk(SQLModel, table=True):
    __tablename__ = "video_index_chunks"
    __table_args__ = (
        UniqueConstraint("asset_id", "chunk_index", name="uq_video_index_chunks_asset_index"),
    )

    chunk_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    chunk_index: int = Field(nullable=False)
    start_ms: int = Field(nullable=False)
    end_ms: int = Field(nullable=False)
    status: str = Field(default="pending", nullable=False)
    worker_id: str | None = Field(default=None, nullable=True)
    claimed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    lease_expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    error_message: str | None = Field(default=None, nullable=True)
    anchor_phash: str | None = Field(default=None, nullable=True)
    scene_start_ms: int | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AssetMetadata(SQLModel, table=True):
    __tablename__ = "asset_metadata"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "model_id",
            "model_version",
            name="uq_asset_metadata_asset_model_version",
        ),
    )

    metadata_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", index=True)
    model_id: str = Field(nullable=False)
    model_version: str = Field(nullable=False)
    generated_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    data: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )



class AssetOcr(SQLModel, table=True):
    """The text read in a clip's image (ADR-016 phase 3): one row per clip,
    apart from its description so describing it again leaves this alone.
    has_text false with text "" means the model found none."""

    __tablename__ = "asset_ocr"

    asset_id: str = Field(foreign_key="assets.asset_id", primary_key=True)
    text: str = Field(default="", nullable=False)
    has_text: bool = Field(nullable=False)
    model_id: str = Field(default="", nullable=False)
    generated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class AssetEmbedding(SQLModel, table=True):
    __tablename__ = "asset_embeddings"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "model_id",
            "model_version",
            name="uq_asset_embeddings_asset_model_version",
        ),
    )

    embedding_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    model_id: str = Field(nullable=False)
    model_version: str = Field(nullable=False)
    embedding_vector: Any = Field(
        sa_column=Column(Vector(), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# Projects (ADR-006)
# ---------------------------------------------------------------------------


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    project_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    description: str | None = Field(default=None, nullable=True)
    cover_asset_id: str | None = Field(
        default=None, foreign_key="assets.asset_id", nullable=True
    )
    owner_user_id: str | None = Field(default=None, nullable=True)
    visibility: str = Field(default="private", nullable=False)  # private | shared | public
    sort_order: str = Field(default="manual", nullable=False)
    type: str = Field(default="static", nullable=False)  # static | smart
    saved_query: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    status: str = Field(default="active", nullable=False)  # active | archived
    archived_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    # In the trash when set; separate from status, so restore puts it back
    # active or archived as it was.
    deleted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ProjectAsset(SQLModel, table=True):
    __tablename__ = "project_assets"

    project_id: str = Field(
        foreign_key="projects.project_id", primary_key=True, nullable=False
    )
    asset_id: str = Field(
        foreign_key="assets.asset_id", primary_key=True, nullable=False
    )
    position: int = Field(nullable=False)
    added_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# Ratings (ADR-007)
# ---------------------------------------------------------------------------


VALID_COLORS = {"red", "orange", "yellow", "green", "blue", "purple"}


class AssetRating(SQLModel, table=True):
    __tablename__ = "asset_ratings"
    __table_args__ = (
        CheckConstraint("stars >= 0 AND stars <= 5", name="ck_asset_ratings_stars"),
        CheckConstraint(
            "color IS NULL OR color IN ('red', 'orange', 'yellow', 'green', 'blue', 'purple')",
            name="ck_asset_ratings_color",
        ),
    )

    user_id: str = Field(primary_key=True, nullable=False)
    asset_id: str = Field(
        foreign_key="assets.asset_id", primary_key=True, nullable=False
    )
    favorite: bool = Field(default=False, nullable=False)
    stars: int = Field(default=0, nullable=False)
    color: str | None = Field(default=None, nullable=True)
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SavedView(SQLModel, table=True):
    __tablename__ = "saved_views"

    view_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    query_params: str = Field(nullable=False)
    icon: str | None = Field(default=None, nullable=True)
    owner_user_id: str = Field(nullable=False, index=True)
    position: int = Field(default=0, nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SystemMetadata(SQLModel, table=True):
    __tablename__ = "system_metadata"

    key: str = Field(primary_key=True)
    value: str = Field(nullable=False)
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# Phase 2: Face detection & person recognition (ADR-009)
# ---------------------------------------------------------------------------


class Face(SQLModel, table=True):
    __tablename__ = "faces"

    face_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    bounding_box_json: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    embedding_vector: Any = Field(
        default=None,
        sa_column=Column(Vector(512), nullable=True),
    )
    detection_confidence: float | None = Field(default=None, nullable=True)
    detection_model: str = Field(default="insightface", nullable=False)
    detection_model_version: str = Field(default="buffalo_l", nullable=False)
    # Which model embedded it: embeddings are compared only within one model.
    embedding_model: str = Field(default="buffalo_l", nullable=False)
    person_id: str | None = Field(
        default=None,
        foreign_key="people.person_id",
        nullable=True,
    )
    crop_key: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Person(SQLModel, table=True):
    __tablename__ = "people"

    person_id: str = Field(primary_key=True)
    display_name: str = Field(nullable=False)
    created_by_user: bool = Field(default=True, nullable=False)
    centroid_vector: Any = Field(
        default=None,
        sa_column=Column(Vector(512), nullable=True),
    )
    confirmation_count: int = Field(default=0, nullable=False)
    dismissed: bool = Field(default=False, nullable=False)
    representative_face_id: str | None = Field(
        default=None, foreign_key="faces.face_id", nullable=True
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class VideoFacetRow(SQLModel, table=True):
    """One ffprobe pass per video: what an editor export needs (ADR-016).

    Derived data; re-probing replaces the row.
    """

    __tablename__ = "video_facets"

    asset_id: str = Field(foreign_key="assets.asset_id", primary_key=True)
    duration_sec: float | None = Field(default=None, nullable=True)
    container: str | None = Field(default=None, nullable=True)
    video_codec: str | None = Field(default=None, nullable=True)
    width: int | None = Field(default=None, nullable=True)  # display width, rotation applied
    height: int | None = Field(default=None, nullable=True)
    rotation: int = Field(default=0, nullable=False)
    frame_rate_num: int | None = Field(default=None, nullable=True)
    frame_rate_den: int | None = Field(default=None, nullable=True)
    start_timecode: str | None = Field(default=None, nullable=True)
    drop_frame: bool | None = Field(default=None, nullable=True)
    audio_codec: str | None = Field(default=None, nullable=True)
    audio_channels: int | None = Field(default=None, nullable=True)
    audio_sample_rate: int | None = Field(default=None, nullable=True)
    probed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ArtifactLineage(SQLModel, table=True):
    """How one clip's artifact of one kind was made (ADR-016 phase 3): the
    producer, its version, the hash of the settings that change its output,
    and the source file's SHA-256 it was made from. When any of them differs
    from what's registered now (src/shared/producers.py), the artifact is
    stale. Faces and scenes count as one set per clip.

    outcome: "ok"; "empty" (made, and there was nothing: no audio, no faces,
    no text); "failed" (tried and failed: error, attempts, retry_at)."""

    __tablename__ = "artifact_lineage"

    asset_id: str = Field(foreign_key="assets.asset_id", primary_key=True)
    artifact: str = Field(primary_key=True)
    producer: str = Field(nullable=False)
    producer_version: str = Field(nullable=False)
    settings_hash: str = Field(nullable=False)
    source_sha256: str | None = Field(default=None, nullable=True)
    produced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    outcome: str = Field(default="ok", nullable=False)
    error: str | None = Field(default=None, nullable=True)
    attempts: int = Field(default=0, nullable=False)
    retry_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class IgnoredFile(SQLModel, table=True):
    """A file whose trash the user emptied. Its asset row is gone, but scans
    and ingest keep skipping the path while the file is still on disk."""

    __tablename__ = "ignored_files"

    library_id: str = Field(foreign_key="libraries.library_id", primary_key=True)
    rel_path: str = Field(primary_key=True)
    sha256: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class FacePersonRejection(SQLModel, table=True):
    """"This face is not this person" — recorded when a user un-assigns a
    face, so auto-assignment never puts it back."""

    __tablename__ = "face_person_rejections"

    face_id: str = Field(foreign_key="faces.face_id", primary_key=True)
    person_id: str = Field(foreign_key="people.person_id", primary_key=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class FacePersonMatch(SQLModel, table=True):
    __tablename__ = "face_person_matches"

    match_id: str = Field(primary_key=True)
    face_id: str = Field(foreign_key="faces.face_id", nullable=False)
    person_id: str = Field(foreign_key="people.person_id", nullable=False)
    confidence: float | None = Field(default=None, nullable=True)
    confirmed: bool = Field(default=False, nullable=False)
    confirmed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
