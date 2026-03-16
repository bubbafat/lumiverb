"""Tenant database models: libraries, assets, scenes, metadata, workers, Phase 2 stubs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, DateTime, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.core.utils import utcnow


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------


class Library(SQLModel, table=True):
    __tablename__ = "libraries"

    library_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    root_path: str = Field(nullable=False)
    status: str = Field(default="active", nullable=False)
    scan_status: str = Field(default="idle", nullable=False)
    last_scan_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_scan_error: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    vision_model_id: str = Field(default="", nullable=False)


class Scan(SQLModel, table=True):
    __tablename__ = "scans"

    scan_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False)
    status: str = Field(default="running", nullable=False)
    root_path_override: str | None = Field(default=None, nullable=True)
    worker_id: str | None = Field(default=None, nullable=True)
    heartbeat_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    files_discovered: int | None = Field(default=None, nullable=True)
    files_added: int | None = Field(default=None, nullable=True)
    files_updated: int | None = Field(default=None, nullable=True)
    files_skipped: int | None = Field(default=None, nullable=True)
    files_missing: int | None = Field(default=None, nullable=True)
    error_message: str | None = Field(default=None, nullable=True)
    started_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class Asset(SQLModel, table=True):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("library_id", "rel_path", name="uq_assets_library_rel_path"),)

    asset_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False)
    last_scan_id: str | None = Field(default=None, foreign_key="scans.scan_id", nullable=True)
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
    duration_ms: int | None = Field(default=None, nullable=True)
    duration_sec: float | None = Field(default=None, nullable=True)
    captured_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    proxy_key: str | None = Field(default=None, nullable=True)
    thumbnail_key: str | None = Field(default=None, nullable=True)
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
    error_message: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class VideoScene(SQLModel, table=True):
    __tablename__ = "video_scenes"

    scene_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    scene_index: int = Field(nullable=False)
    start_ms: int = Field(nullable=False)
    end_ms: int = Field(nullable=False)
    rep_frame_ms: int = Field(nullable=False)
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
        sa_column=Column(Vector(512), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SearchSyncQueue(SQLModel, table=True):
    __tablename__ = "search_sync_queue"

    sync_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    scene_id: str | None = Field(default=None, foreign_key="video_scenes.scene_id", nullable=True)
    operation: str = Field(nullable=False)
    status: str = Field(default="pending", nullable=False)
    processing_started_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class WorkerJob(SQLModel, table=True):
    __tablename__ = "worker_jobs"

    job_id: str = Field(primary_key=True)
    job_type: str = Field(nullable=False)
    asset_id: str | None = Field(default=None, foreign_key="assets.asset_id", nullable=True)
    scene_id: str | None = Field(default=None, foreign_key="video_scenes.scene_id", nullable=True)
    status: str = Field(default="pending", nullable=False)
    priority: int = Field(default=10, nullable=False)
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
    fail_count: int = Field(default=0, nullable=False)
    error_message: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
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
# Phase 2 stub tables — create now, leave empty, never query in Phase 1
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
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Person(SQLModel, table=True):
    __tablename__ = "people"

    person_id: str = Field(primary_key=True)
    display_name: str = Field(nullable=False)
    created_by_user: bool = Field(default=True, nullable=False)
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
