"""Tenant database models: libraries, assets, scenes, metadata, workers, Phase 2 stubs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------


class Library(SQLModel, table=True):
    __tablename__ = "libraries"

    library_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    root_path: str = Field(nullable=False)
    scan_status: str = Field(default="idle", nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Asset(SQLModel, table=True):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("library_id", "rel_path", name="uq_assets_library_rel_path"),)

    asset_id: str = Field(primary_key=True)
    library_id: str = Field(foreign_key="libraries.library_id", nullable=False)
    rel_path: str = Field(nullable=False)
    sha256: str | None = Field(default=None, nullable=True)
    file_size: int = Field(nullable=False)
    media_type: str = Field(nullable=False)
    width: int | None = Field(default=None, nullable=True)
    height: int | None = Field(default=None, nullable=True)
    duration_ms: int | None = Field(default=None, nullable=True)
    captured_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    proxy_key: str | None = Field(default=None, nullable=True)
    thumbnail_key: str | None = Field(default=None, nullable=True)
    availability: str = Field(default="online", nullable=False)
    status: str = Field(default="pending", nullable=False)
    error_message: str | None = Field(default=None, nullable=True)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
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
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AssetMetadata(SQLModel, table=True):
    __tablename__ = "asset_metadata"

    asset_id: str = Field(primary_key=True, foreign_key="assets.asset_id")
    exif_json: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    sharpness_score: float | None = Field(default=None, nullable=True)
    face_count: int | None = Field(default=0, nullable=True)
    ai_description: str | None = Field(default=None, nullable=True)
    ai_tags: list[str] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    ai_ocr_text: str | None = Field(default=None, nullable=True)
    ai_description_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    embedding_vector: Any = Field(
        default=None,
        sa_column=Column(Vector(512), nullable=True),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SearchSyncQueue(SQLModel, table=True):
    __tablename__ = "search_sync_queue"

    sync_id: str = Field(primary_key=True)
    asset_id: str = Field(foreign_key="assets.asset_id", nullable=False)
    scene_id: str | None = Field(default=None, foreign_key="video_scenes.scene_id", nullable=True)
    operation: str = Field(nullable=False)
    status: str = Field(default="pending", nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class WorkerJob(SQLModel, table=True):
    __tablename__ = "worker_jobs"

    job_id: str = Field(primary_key=True)
    job_type: str = Field(nullable=False)
    asset_id: str | None = Field(default=None, foreign_key="assets.asset_id", nullable=True)
    scene_id: str | None = Field(default=None, foreign_key="video_scenes.scene_id", nullable=True)
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
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class SystemMetadata(SQLModel, table=True):
    __tablename__ = "system_metadata"

    key: str = Field(primary_key=True)
    value: str = Field(nullable=False)
    updated_at: datetime = Field(
        default_factory=_utcnow,
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
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Person(SQLModel, table=True):
    __tablename__ = "people"

    person_id: str = Field(primary_key=True)
    display_name: str = Field(nullable=False)
    created_by_user: bool = Field(default=True, nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow,
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
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
