"""Repository classes for the tenant database. All take session: Session in constructor."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta

from sqlalchemy import and_, bindparam, column, func, insert, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import text as sa_text
from sqlmodel import Session, select

from src.core.io_utils import normalize_path_prefix
from src.core.utils import utcnow
from src.core import asset_status
from src.models.similarity import SimilarityScope
from src.models.tenant import (
    Asset,
    AssetEmbedding,
    AssetMetadata,
    Library,
    LibraryPathFilter,
    TenantPathFilterDefault,
    VideoIndexChunk,
    VideoScene,
)
from ulid import ULID

# Canonical view for non-trashed assets. Use in raw SQL (e.g. FROM active_assets).
ACTIVE_ASSETS = "active_assets"


def _active_assets_subquery():
    # active_assets is a DB view, not a SQLModel table.
    return (
        sa_text("SELECT asset_id, library_id, rel_path FROM active_assets")
        .columns(
            column("asset_id"),
            column("library_id"),
            column("rel_path"),
        )
        .subquery("active_a")
    )



class LibraryRepository:
    """Repository for libraries table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str, root_path: str) -> Library:
        """Generate library_id as lib_ + ULID(), insert, return Library."""
        library_id = "lib_" + str(ULID())
        library = Library(
            library_id=library_id,
            name=name,
            root_path=root_path,
            scan_status="idle",
        )
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def bump_revision(self, library_id: str) -> None:
        """Atomically increment the library revision counter.

        Uses a separate short transaction to avoid holding a row lock
        on the libraries table for the duration of long ingest transactions.
        """
        engine = self._session.get_bind()
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE libraries SET revision = revision + 1,"
                    " last_scan_at = now() WHERE library_id = :lid"
                ),
                {"lid": library_id},
            )
            conn.commit()

    def get_by_id(self, library_id: str) -> Library | None:
        """Return library by id or None."""
        stmt = select(Library).where(Library.library_id == library_id)
        return self._session.exec(stmt).first()

    def get_by_name(self, name: str) -> Library | None:
        """Return library by name or None."""
        stmt = select(Library).where(Library.name == name)
        return self._session.exec(stmt).first()

    def list_all(self, include_trashed: bool = False) -> list[Library]:
        """Return all libraries. By default exclude status='trashed'; if include_trashed=True return all."""
        stmt = select(Library)
        if not include_trashed:
            stmt = stmt.where(Library.status != "trashed")
        return list(self._session.exec(stmt).all())

    def get_trashed(self) -> list[Library]:
        """Return all libraries with status='trashed'."""
        stmt = select(Library).where(Library.status == "trashed")
        return list(self._session.exec(stmt).all())

    def trash(self, library_id: str) -> Library:
        """Set library status to trashed, cancel pending/claimed worker jobs for its assets, return updated library."""
        library = self.get_by_id(library_id)
        if library is None:
            raise ValueError(f"Library not found: {library_id}")
        if library.status == "trashed":
            raise ValueError(f"Library already trashed: {library_id}")
        # Soft-delete all assets in this library
        self._session.execute(
            text(
                "UPDATE assets SET deleted_at = :now WHERE library_id = :library_id AND deleted_at IS NULL"
            ),
            {"library_id": library_id, "now": utcnow()},
        )
        library.status = "trashed"
        library.updated_at = utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def hard_delete(self, library_id: str) -> None:
        """Permanently delete library and all related data in FK-safe order. Single transaction."""
        # Order: asset_metadata, video_scenes, video_index_chunks, assets, libraries
        params = {"library_id": library_id}
        self._session.execute(
            text(
                """
                DELETE FROM asset_metadata
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM asset_embeddings
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM video_scenes
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM video_index_chunks
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(text("DELETE FROM assets WHERE library_id = :library_id"), params)
        self._session.execute(
            text("DELETE FROM library_path_filters WHERE library_id = :library_id"), params
        )
        self._session.execute(text("DELETE FROM libraries WHERE library_id = :library_id"), params)
        self._session.commit()


class PathFilterRepository:
    """Repository for library_path_filters and tenant_path_filter_defaults."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_library(self, library_id: str) -> list[LibraryPathFilter]:
        """Return all path filters for a library."""
        stmt = select(LibraryPathFilter).where(LibraryPathFilter.library_id == library_id)
        return list(self._session.exec(stmt).all())

    def add_for_library(self, library_id: str, type: str, pattern: str) -> LibraryPathFilter:
        """Add a filter for a library. filter_id = lpf_ + ULID."""
        filter_id = "lpf_" + str(ULID())
        row = LibraryPathFilter(
            filter_id=filter_id,
            library_id=library_id,
            type=type,
            pattern=pattern,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete_for_library(self, filter_id: str, library_id: str) -> bool:
        """Delete a filter by id and library_id. Returns False if not found."""
        stmt = select(LibraryPathFilter).where(
            LibraryPathFilter.filter_id == filter_id,
            LibraryPathFilter.library_id == library_id,
        )
        row = self._session.exec(stmt).first()
        if row is None:
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    def list_defaults(self, tenant_id: str) -> list[TenantPathFilterDefault]:
        """Return all tenant path filter defaults."""
        stmt = select(TenantPathFilterDefault).where(
            TenantPathFilterDefault.tenant_id == tenant_id
        )
        return list(self._session.exec(stmt).all())

    def add_default(self, tenant_id: str, type: str, pattern: str) -> TenantPathFilterDefault:
        """Add a tenant default. default_id = tpfd_ + ULID."""
        default_id = "tpfd_" + str(ULID())
        row = TenantPathFilterDefault(
            default_id=default_id,
            tenant_id=tenant_id,
            type=type,
            pattern=pattern,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete_default(self, default_id: str, tenant_id: str) -> bool:
        """Delete a tenant default by id and tenant_id. Returns False if not found."""
        stmt = select(TenantPathFilterDefault).where(
            TenantPathFilterDefault.default_id == default_id,
            TenantPathFilterDefault.tenant_id == tenant_id,
        )
        row = self._session.exec(stmt).first()
        if row is None:
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    def copy_defaults_to_library(self, tenant_id: str, library_id: str) -> int:
        """Copy current tenant defaults into library_path_filters. Returns count copied. New filter_id ULIDs are generated."""
        defaults = self.list_defaults(tenant_id)
        if not defaults:
            return 0
        count = 0
        for d in defaults:
            self.add_for_library(library_id=library_id, type=d.type, pattern=d.pattern)
            count += 1
        return count

class AssetRepository:
    """Repository for assets table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_library_and_rel_path(self, library_id: str, rel_path: str) -> Asset | None:
        """Return asset by (library_id, rel_path) or None."""
        stmt = select(Asset).where(
            Asset.library_id == library_id,
            Asset.rel_path == rel_path,
        )
        return self._session.exec(stmt).first()
    def create_asset(
        self,
        library_id: str,
        rel_path: str,
        file_size: int,
        file_mtime: datetime | None,
        media_type: str,
    ) -> Asset:
        """Create a new asset."""
        asset_id = "ast_" + str(ULID())
        asset = Asset(
            asset_id=asset_id,
            library_id=library_id,
            rel_path=rel_path,
            file_size=file_size,
            file_mtime=file_mtime,
            media_type=media_type,
            status=asset_status.PENDING,
            availability="online",
        )
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def get_by_id(self, asset_id: str) -> Asset | None:
        """Return active (non-trashed) asset by id or None."""
        stmt = select(Asset).where(Asset.asset_id == asset_id).where(Asset.deleted_at.is_(None))
        return self._session.exec(stmt).first()

    def list_pending_by_library(self, library_id: str) -> list[Asset]:
        """Return all active (non-trashed) assets in library with status='pending'."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .where(Asset.status == "pending")
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def list_by_library(self, library_id: str) -> list[Asset]:
        """Return all active (non-trashed) assets in library."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def list_ids_matching_pattern(self, library_id: str, pattern: str) -> list[str]:
        """Return asset_ids of active assets whose rel_path matches a glob pattern."""
        from src.core.path_filter import _glob_match

        stmt = (
            select(Asset.asset_id, Asset.rel_path)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
        )
        return [
            row[0] for row in self._session.execute(stmt).all()
            if _glob_match(pattern, row[1])
        ]

    def count_by_library(self, library_id: str) -> int:
        """Return total active asset count for library."""
        result = self._session.execute(
            text(
                "SELECT COUNT(*)::int FROM active_assets WHERE library_id = :library_id"
            ),
            {"library_id": library_id},
        )
        return int(result.scalar() or 0)

    def count_all_for_libraries(self, library_ids: list[str]) -> int:
        """Return total active asset count across all given libraries in a single query."""
        if not library_ids:
            return 0
        stmt = text(
            "SELECT COUNT(*)::int FROM active_assets WHERE library_id IN :library_ids"
        ).bindparams(bindparam("library_ids", expanding=True))
        result = self._session.execute(stmt, {"library_ids": library_ids})
        return int(result.scalar() or 0)

    # Columns allowed for sorting.
    SORTABLE_COLUMNS = {
        "asset_id", "taken_at", "created_at", "file_size",
        "iso", "aperture", "focal_length", "rel_path",
    }

    def page_by_library(
        self,
        library_id: str,
        after: str | None,
        limit: int,
        path_prefix: str | None = None,
        tag: str | None = None,
        missing_vision: bool = False,
        missing_embeddings: bool = False,
        *,
        sort: str = "taken_at",
        direction: str = "desc",
        media_types: list[str] | None = None,
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
    ) -> list[Asset]:
        """Keyset pagination with composite cursor, sorting, and filtering.

        The ``after`` parameter is either:
        - A plain asset_id string (legacy callers, sort=asset_id implied).
        - A base64-encoded JSON ``{"v": <sort_value>, "id": <asset_id>}``.

        Returns assets ordered by the requested sort column with asset_id as
        tiebreaker.  Nulls always sort last.
        """
        import base64 as _b64
        import math as _math

        sort_col = sort if sort in self.SORTABLE_COLUMNS else "taken_at"
        is_desc = direction.lower() == "desc"
        cmp_op = "<" if is_desc else ">"
        order_dir = "DESC" if is_desc else "ASC"

        conditions = ["a.library_id = :library_id"]
        params: dict[str, object] = {
            "library_id": library_id,
            "limit": limit,
        }

        # --- Path prefix ---
        if path_prefix:
            conditions.append(
                "(a.rel_path = :path_prefix OR a.rel_path LIKE :path_prefix_like)"
            )
            params["path_prefix"] = path_prefix
            params["path_prefix_like"] = path_prefix + "/%"

        # --- Composite cursor ---
        if after is not None:
            cursor_value = None
            cursor_id = after  # default: plain asset_id
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_value = decoded["v"]
                cursor_id = decoded["id"]
            except Exception:
                # Legacy plain asset_id cursor — treat as sort=asset_id
                if sort_col != "asset_id":
                    # Fallback: just use asset_id comparison
                    sort_col = "asset_id"

            if sort_col == "asset_id":
                conditions.append(f"a.asset_id {cmp_op} :cursor_id")
                params["cursor_id"] = cursor_id
            else:
                # Row-value comparison for composite cursor.
                # NULLs sort last: rows with NULL sort_col come after non-NULL rows.
                conditions.append(f"""(
                    CASE
                        WHEN :cursor_value IS NULL THEN
                            a.{sort_col} IS NOT NULL
                            OR (a.{sort_col} IS NULL AND a.asset_id {cmp_op} :cursor_id)
                        WHEN a.{sort_col} IS NULL THEN
                            FALSE
                        ELSE
                            (a.{sort_col}, a.asset_id) {cmp_op} (:cursor_value, :cursor_id)
                    END
                )""")
                params["cursor_value"] = cursor_value
                params["cursor_id"] = cursor_id

        # --- Tag / missing_vision ---
        if tag is not None:
            conditions.append("m.tags @> jsonb_build_array(:tag)")
            params["tag"] = tag
        if missing_vision:
            conditions.append("m.tags IS NULL")
        if missing_embeddings:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id)"
            )

        # --- Media type filter ---
        if media_types:
            clauses = []
            if "image" in media_types:
                clauses.append("a.media_type = 'image'")
            if "video" in media_types:
                clauses.append("a.media_type = 'video'")
            if clauses:
                conditions.append(f"({' OR '.join(clauses)})")

        # --- Camera / lens filters ---
        if camera_make:
            conditions.append("a.camera_make = :camera_make")
            params["camera_make"] = camera_make
        if camera_model:
            conditions.append("a.camera_model = :camera_model")
            params["camera_model"] = camera_model
        if lens_model:
            conditions.append("a.lens_model = :lens_model")
            params["lens_model"] = lens_model

        # --- EXIF range filters ---
        if iso_min is not None:
            conditions.append("a.iso >= :iso_min")
            params["iso_min"] = iso_min
        if iso_max is not None:
            conditions.append("a.iso <= :iso_max")
            params["iso_max"] = iso_max
        if exposure_min_us is not None:
            conditions.append("a.exposure_time_us >= :exposure_min_us")
            params["exposure_min_us"] = exposure_min_us
        if exposure_max_us is not None:
            conditions.append("a.exposure_time_us <= :exposure_max_us")
            params["exposure_max_us"] = exposure_max_us
        if aperture_min is not None:
            conditions.append("a.aperture >= :aperture_min")
            params["aperture_min"] = aperture_min
        if aperture_max is not None:
            conditions.append("a.aperture <= :aperture_max")
            params["aperture_max"] = aperture_max
        if focal_length_min is not None:
            conditions.append("a.focal_length >= :focal_length_min")
            params["focal_length_min"] = focal_length_min
        if focal_length_max is not None:
            conditions.append("a.focal_length <= :focal_length_max")
            params["focal_length_max"] = focal_length_max

        # --- Exposure data filter ---
        if has_exposure is True:
            conditions.append(
                "(a.iso IS NOT NULL OR a.exposure_time_us IS NOT NULL OR a.aperture IS NOT NULL)"
            )
        elif has_exposure is False:
            conditions.append(
                "a.iso IS NULL AND a.exposure_time_us IS NULL AND a.aperture IS NULL"
            )

        # --- GPS filters ---
        if has_gps:
            conditions.append("a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL")
        if near_lat is not None and near_lon is not None:
            lat_delta = near_radius_km / 111.0
            lon_delta = near_radius_km / (111.0 * _math.cos(_math.radians(near_lat)))
            conditions.append("a.gps_lat BETWEEN :min_lat AND :max_lat")
            conditions.append("a.gps_lon BETWEEN :min_lon AND :max_lon")
            params["min_lat"] = near_lat - lat_delta
            params["max_lat"] = near_lat + lat_delta
            params["min_lon"] = near_lon - lon_delta
            params["max_lon"] = near_lon + lon_delta

        # --- Build query ---
        join_metadata = tag is not None or missing_vision
        where_sql = " AND ".join(conditions)

        lateral_join = ""
        if join_metadata:
            lateral_join = """
            LEFT JOIN LATERAL (
                SELECT data->'tags' AS tags
                FROM asset_metadata
                WHERE asset_id = a.asset_id
                ORDER BY generated_at DESC
                LIMIT 1
            ) m ON TRUE
            """

        if sort_col == "asset_id":
            order_clause = f"a.asset_id {order_dir}"
        else:
            order_clause = f"a.{sort_col} {order_dir} NULLS LAST, a.asset_id {order_dir}"

        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            {lateral_join}
            WHERE {where_sql}
            ORDER BY {order_clause}
            LIMIT :limit
        """
        result = self._session.execute(text(id_sql).bindparams(**params))
        asset_ids = [row[0] for row in result.all()]
        if not asset_ids:
            return []
        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        assets_by_id = {a.asset_id: a for a in self._session.exec(stmt).all()}
        return [assets_by_id[aid] for aid in asset_ids if aid in assets_by_id]

    def list_rel_paths_for_library_non_deleted(self, library_id: str) -> list[str]:
        """Return rel_path for all active (non-trashed) assets in library."""
        stmt = (
            select(Asset.rel_path)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def list_all(self) -> list[Asset]:
        """Return all active (non-trashed) assets (all libraries)."""
        stmt = select(Asset).where(Asset.deleted_at.is_(None))
        return list(self._session.exec(stmt).all())

    def trash(self, asset_id: str) -> bool:
        """Set deleted_at = now(). Returns False if not found or already trashed."""
        asset = self._session.get(Asset, asset_id)
        if asset is None or asset.deleted_at is not None:
            return False
        asset.deleted_at = utcnow()
        self._session.add(asset)
        self._session.commit()
        return True

    def trash_many(self, asset_ids: list[str]) -> tuple[list[str], list[str]]:
        """Bulk trash. Returns (trashed_ids, not_found_ids)."""
        if not asset_ids:
            return [], []
        now = utcnow()
        result = self._session.execute(
            text(
                """
                UPDATE assets SET deleted_at = :now
                WHERE asset_id = ANY(:ids) AND deleted_at IS NULL
                RETURNING asset_id
                """
            ),
            {"now": now, "ids": asset_ids},
        )
        trashed = [row[0] for row in result.fetchall()]
        not_found = [aid for aid in asset_ids if aid not in trashed]
        self._session.commit()
        return (trashed, not_found)

    def restore(self, asset_id: str) -> bool:
        """Clear deleted_at. Returns False if not found or not trashed."""
        asset = self._session.get(Asset, asset_id)
        if asset is None or asset.deleted_at is None:
            return False
        asset.deleted_at = None
        self._session.add(asset)
        self._session.commit()
        return True

    def list_trashed(
        self,
        asset_ids: list[str] | None = None,
        trashed_before: datetime | None = None,
    ) -> list[Asset]:
        """Return trashed assets matching the given filters."""
        stmt = select(Asset).where(Asset.deleted_at.isnot(None))
        if asset_ids is not None:
            stmt = stmt.where(Asset.asset_id.in_(asset_ids))
        if trashed_before is not None:
            stmt = stmt.where(Asset.deleted_at < trashed_before)
        return list(self._session.exec(stmt).all())

    def permanently_delete(self, asset_ids: list[str]) -> int:
        """
        Hard delete trashed assets and all related rows in FK-safe order.
        Only deletes rows where deleted_at IS NOT NULL. Returns count of deleted asset rows.
        """
        if not asset_ids:
            return 0
        params = {"asset_ids": asset_ids}
        self._session.execute(
            text("DELETE FROM asset_metadata WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM asset_embeddings WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM video_scenes WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM video_index_chunks WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        result = self._session.execute(
            text(
                "DELETE FROM assets WHERE asset_id = ANY(:asset_ids) AND deleted_at IS NOT NULL RETURNING asset_id"
            ),
            params,
        )
        deleted_count = len(result.fetchall())
        self._session.commit()
        return deleted_count

    def update_proxy(
        self,
        asset_id: str,
        proxy_key: str,
        thumbnail_key: str,
        width: int,
        height: int,
        proxy_sha256: str | None = None,
        thumbnail_sha256: str | None = None,
    ) -> Asset:
        """
        Update asset proxy_key, thumbnail_key, width, height, status='proxy_ready', updated_at.

        If proxy_sha256/thumbnail_sha256 are provided (non-None), persist them too.
        """
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = proxy_key
        asset.thumbnail_key = thumbnail_key
        if proxy_sha256 is not None:
            asset.proxy_sha256 = proxy_sha256
        if thumbnail_sha256 is not None:
            asset.thumbnail_sha256 = thumbnail_sha256
        asset.width = width
        asset.height = height
        asset.status = asset_status.PROXY_READY
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def set_status(self, asset_id: str, status: str) -> None:
        """Set asset.status to the given value and bump updated_at."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.status = status
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_video_preview(
        self,
        asset_id: str,
        video_preview_key: str,
    ) -> None:
        """Record video preview key and generated_at timestamp."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.video_preview_key = video_preview_key
        asset.video_preview_generated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_proxy_artifact(
        self,
        asset_id: str,
        key: str,
        sha256: str,
        width: int | None,
        height: int | None,
    ) -> None:
        """Set proxy_key and proxy_sha256. Width/height updated only if non-None.

        Does NOT touch thumbnail_key or advance asset.status — status transitions
        are the job-complete path's responsibility, not the upload endpoint's.
        """
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = key
        asset.proxy_sha256 = sha256
        if width is not None:
            asset.width = width
        if height is not None:
            asset.height = height
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_thumbnail_artifact(self, asset_id: str, key: str, sha256: str) -> None:
        """Set thumbnail_key and thumbnail_sha256. Does NOT touch proxy_key or status."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.thumbnail_key = key
        asset.thumbnail_sha256 = sha256
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_video_indexed(self, asset_id: str) -> None:
        """Set asset.video_indexed = True. Used when video-vision job completes."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.video_indexed = True
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def reset_video_indexed_for_library(self, library_id: str) -> int:
        """Set video_indexed = False for all video assets in a library. Returns count updated."""
        from sqlalchemy import update as sa_update
        result = self._session.exec(  # type: ignore[call-overload]
            sa_update(Asset)
            .where(Asset.library_id == library_id, Asset.media_type == "video")
            .values(video_indexed=False)
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]

    def update_thumbnail_key(self, asset_id: str, thumbnail_key: str) -> None:
        """Record a thumbnail_key on the asset."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.thumbnail_key = thumbnail_key
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def update_exif(
        self,
        asset_id: str,
        sha256: str | None,
        exif: dict,
        camera_make: str | None,
        camera_model: str | None,
        taken_at: str | None,
        gps_lat: float | None,
        gps_lon: float | None,
        duration_sec: float | None = None,
        iso: int | None = None,
        exposure_time_us: int | None = None,
        aperture: float | None = None,
        focal_length: float | None = None,
        focal_length_35mm: float | None = None,
        lens_model: str | None = None,
        flash_fired: bool | None = None,
        orientation: int | None = None,
    ) -> None:
        """Update EXIF fields on asset record."""
        taken_at_dt: datetime | None = None
        if taken_at:
            try:
                taken_at_dt = datetime.fromisoformat(taken_at)
            except ValueError:
                pass
        self._session.execute(
            text(
                """
                UPDATE assets SET
                    sha256 = :sha256,
                    exif = :exif,
                    exif_extracted_at = :now,
                    camera_make = :camera_make,
                    camera_model = :camera_model,
                    taken_at = :taken_at,
                    gps_lat = :gps_lat,
                    gps_lon = :gps_lon,
                    duration_sec = COALESCE(:duration_sec, duration_sec),
                    iso = :iso,
                    exposure_time_us = :exposure_time_us,
                    aperture = :aperture,
                    focal_length = :focal_length,
                    focal_length_35mm = :focal_length_35mm,
                    lens_model = :lens_model,
                    flash_fired = :flash_fired,
                    orientation = :orientation
                WHERE asset_id = :asset_id
                """
            ),
            {
                "sha256": sha256,
                "exif": json.dumps(exif) if exif else None,
                "now": utcnow(),
                "camera_make": camera_make,
                "camera_model": camera_model,
                "taken_at": taken_at_dt,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "duration_sec": duration_sec,
                "iso": iso,
                "exposure_time_us": exposure_time_us,
                "aperture": aperture,
                "focal_length": focal_length,
                "focal_length_35mm": focal_length_35mm,
                "lens_model": lens_model,
                "flash_fired": flash_fired,
                "orientation": orientation,
                "asset_id": asset_id,
            },
        )
        self._session.commit()

    def get_by_ids(self, asset_ids: list[str]) -> list[Asset]:
        """Return active (non-trashed) assets for a list of asset_ids. Order not guaranteed."""
        if not asset_ids:
            return []
        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def get_states(self, asset_ids: list[str]) -> dict[str, dict]:
        """Fetch deleted status and proxy_sha256 for a list of asset_ids.

        Returns dict keyed by asset_id. IDs not present in DB are not included.
        Deliberately includes soft-deleted assets — callers must not add a
        deleted_at IS NULL filter here.
        """
        if not asset_ids:
            return {}
        stmt = (
            select(Asset.asset_id, Asset.deleted_at, Asset.proxy_sha256)
            .where(Asset.asset_id.in_(asset_ids))
        )
        rows = self._session.exec(stmt).all()
        return {
            row.asset_id: {
                "deleted": row.deleted_at is not None,
                "proxy_sha256": row.proxy_sha256,
            }
            for row in rows
        }


class AssetMetadataRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
        data: dict,
    ) -> None:
        """
        Insert or update metadata row for (asset_id, model_id, model_version).
        On conflict: update data and generated_at only.
        """
        now = utcnow()
        stmt = pg_insert(AssetMetadata).values(
            metadata_id="meta_" + str(ULID()),
            asset_id=asset_id,
            model_id=model_id,
            model_version=model_version,
            generated_at=now,
            data=data,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_metadata_asset_model_version",
            set_={
                "data": stmt.excluded.data,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        self._session.execute(stmt)
        self._session.commit()

    def get(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
    ) -> AssetMetadata | None:
        stmt = select(AssetMetadata).where(
            AssetMetadata.asset_id == asset_id,
            AssetMetadata.model_id == model_id,
            AssetMetadata.model_version == model_version,
        )
        return self._session.exec(stmt).first()

    def get_latest(self, asset_id: str) -> AssetMetadata | None:
        """Return the most recent metadata row for the asset, regardless of model."""
        stmt = (
            select(AssetMetadata)
            .where(AssetMetadata.asset_id == asset_id)
            .order_by(AssetMetadata.generated_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        return self._session.exec(stmt).first()

    def list_for_asset(self, asset_id: str) -> list[AssetMetadata]:
        stmt = select(AssetMetadata).where(AssetMetadata.asset_id == asset_id)
        return list(self._session.exec(stmt).all())


class AssetEmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
        vector: list[float],
    ) -> None:
        """
        Insert or update the embedding for (asset_id, model_id, model_version).
        Uses ON CONFLICT DO UPDATE on the unique constraint.
        """
        stmt = pg_insert(AssetEmbedding).values(
            embedding_id="emb_" + str(ULID()),
            asset_id=asset_id,
            model_id=model_id,
            model_version=model_version,
            embedding_vector=vector,
            created_at=utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_embeddings_asset_model_version",
            set_={"embedding_vector": vector, "created_at": utcnow()},
        )
        self._session.execute(stmt)
        self._session.commit()

    def get(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
    ) -> AssetEmbedding | None:
        stmt = select(AssetEmbedding).where(
            AssetEmbedding.asset_id == asset_id,
            AssetEmbedding.model_id == model_id,
            AssetEmbedding.model_version == model_version,
        )
        return self._session.exec(stmt).first()

    def find_similar(
        self,
        library_id: str,
        model_id: str,
        model_version: str,
        vector: list[float],
        limit: int,
        offset: int = 0,
        exclude_asset_id: str | None = None,
        scope: SimilarityScope | None = None,
    ) -> list[tuple[str, float]]:
        """
        Return (asset_id, distance) pairs ordered by cosine distance ASC.
        Filters to assets in library_id that are online.
        Optional scope applies extra filters (e.g. date range via taken_at).
        """
        conditions = [
            "a.library_id = :library_id",
            "a.availability = 'online'",
            "ae.model_id = :model_id",
            "ae.model_version = :model_version",
        ]
        params: dict = {
            "vec": str(vector),
            "library_id": library_id,
            "model_id": model_id,
            "model_version": model_version,
            "limit": limit,
            "offset": offset,
        }
        if exclude_asset_id is not None:
            conditions.append("ae.asset_id != :exclude_id")
            params["exclude_id"] = exclude_asset_id
        if scope and scope.date_range:
            dr = scope.date_range
            if dr.from_ts is not None:
                conditions.append(
                    "a.taken_at IS NOT NULL AND a.taken_at >= to_timestamp(:from_ts) AT TIME ZONE 'UTC'"
                )
                params["from_ts"] = dr.from_ts
            if dr.to_ts is not None:
                conditions.append(
                    "a.taken_at IS NOT NULL AND a.taken_at <= to_timestamp(:to_ts) AT TIME ZONE 'UTC'"
                )
                params["to_ts"] = dr.to_ts
        if scope and scope.asset_types and scope.asset_types != "all":
            # scope.asset_types is list["image" | "video"]; match media_type by prefix
            type_patterns = [f"{t}%" for t in scope.asset_types]
            placeholders = [f"a.media_type LIKE :asset_type_pat_{i}" for i in range(len(type_patterns))]
            conditions.append("(" + " OR ".join(placeholders) + ")")
            for i, pat in enumerate(type_patterns):
                params[f"asset_type_pat_{i}"] = pat
        if scope and scope.cameras:
            # OR across (make, model) pairs; within each pair AND
            cam_clauses = []
            for i, c in enumerate(scope.cameras):
                if c.make is not None and c.model is not None:
                    cam_clauses.append(
                        "(a.camera_make = :cam_make_{i} AND a.camera_model = :cam_model_{i})".format(i=i)
                    )
                    params[f"cam_make_{i}"] = c.make
                    params[f"cam_model_{i}"] = c.model
                elif c.make is not None:
                    cam_clauses.append("a.camera_make = :cam_make_{i}".format(i=i))
                    params[f"cam_make_{i}"] = c.make
                elif c.model is not None:
                    cam_clauses.append("a.camera_model = :cam_model_{i}".format(i=i))
                    params[f"cam_model_{i}"] = c.model
            if cam_clauses:
                conditions.append("(" + " OR ".join(cam_clauses) + ")")
        where = " AND ".join(conditions)
        sql = f"""
                SELECT ae.asset_id,
                       ae.embedding_vector <=> CAST(:vec AS vector) AS distance
                FROM asset_embeddings ae
                JOIN active_assets a ON a.asset_id = ae.asset_id
                WHERE {where}
                ORDER BY distance ASC
                LIMIT :limit OFFSET :offset
            """
        rows = self._session.execute(text(sql), params).fetchall()
        return [(r.asset_id, float(r.distance)) for r in rows]


FAILURE_BLOCK_THRESHOLD = 3

class VideoSceneRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_scenes_for_asset(self, asset_id: str) -> list[VideoScene]:
        """Return all scenes for an asset ordered by start_ms."""
        return list(
            self._session.exec(
                select(VideoScene)
                .where(VideoScene.asset_id == asset_id)
                .order_by(VideoScene.start_ms)
            ).all()
        )

    def get_by_id(self, scene_id: str) -> VideoScene | None:
        return self._session.get(VideoScene, scene_id)

    def delete_for_library(self, library_id: str) -> int:
        """Delete all scenes for all video assets in a library. Returns count deleted."""
        from sqlalchemy import delete as sa_delete
        result = self._session.exec(  # type: ignore[call-overload]
            sa_delete(VideoScene).where(
                VideoScene.asset_id.in_(  # type: ignore[attr-defined]
                    select(Asset.asset_id).where(
                        Asset.library_id == library_id,
                        Asset.media_type == "video",
                    )
                )
            )
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]

    def update_vision(
        self,
        scene_id: str,
        model_id: str,
        model_version: str,
        description: str,
        tags: list[str],
    ) -> None:
        """Write vision results back to a scene row."""
        scene = self._session.get(VideoScene, scene_id)
        if scene is None:
            raise ValueError(f"Scene not found: {scene_id}")
        scene.description = description
        scene.tags = tags
        self._session.add(scene)
        self._session.commit()


class VideoIndexChunkRepository:
    """Repository for video_index_chunks and chunk completion (scenes)."""

    CHUNK_DURATION_SEC: float = 30.0
    OVERLAP_SEC: float = 2.0
    LEASE_MINUTES: int = 5

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_chunks_for_asset(
        self,
        asset_id: str,
        duration_sec: float,
    ) -> int:
        """
        Pre-generate all pending chunks for a video asset based on duration.
        Idempotent — skips creation if chunks already exist for this asset.
        Returns count of chunks created.
        """
        existing = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.asset_id == asset_id)
        ).first()
        if existing:
            return 0

        chunks: list[dict] = []
        start = 0.0
        index = 0
        now = utcnow()
        while start < duration_sec:
            end = min(start + self.CHUNK_DURATION_SEC, duration_sec)
            chunks.append({
                "chunk_id": "chk_" + str(ULID()),
                "asset_id": asset_id,
                "chunk_index": index,
                "start_ms": int(start * 1000),
                "end_ms": int(end * 1000),
                "status": "pending",
                "created_at": now,
            })
            start = end
            index += 1

        if chunks:
            self._session.execute(insert(VideoIndexChunk), chunks)
            self._session.commit()
        return len(chunks)

    def claim_next_chunk(
        self,
        asset_id: str,
        worker_id: str,
    ) -> VideoIndexChunk | None:
        """
        Claim the next pending chunk for an asset (lowest chunk_index).
        Also reclaims chunks whose lease has expired and resets failed chunks
        back to pending so they are retried by the current video-index job.
        Returns None if no claimable chunks remain.
        """
        now = utcnow()
        lease_expires = now + timedelta(minutes=self.LEASE_MINUTES)

        # Reset expired-lease claimed chunks and previously-failed chunks back
        # to pending.  Failed chunks must be retried rather than left stuck —
        # all_chunks_complete returns False while any chunk is non-completed,
        # which would permanently prevent video-vision from being enqueued.
        # The video-index retry ceiling is managed externally.
        reclaimable = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                or_(
                    and_(
                        VideoIndexChunk.status == "claimed",
                        VideoIndexChunk.lease_expires_at < now,
                    ),
                    VideoIndexChunk.status == "failed",
                ),
            )
        ).all()
        for chunk in reclaimable:
            chunk.status = "pending"
            chunk.worker_id = None
            chunk.claimed_at = None
            chunk.lease_expires_at = None
            self._session.add(chunk)
        if reclaimable:
            self._session.flush()

        chunk = self._session.exec(
            select(VideoIndexChunk)
            .where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "pending",
            )
            .order_by(VideoIndexChunk.chunk_index)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()

        if chunk is None:
            return None

        chunk.status = "claimed"
        chunk.worker_id = worker_id
        chunk.claimed_at = now
        chunk.lease_expires_at = lease_expires
        self._session.add(chunk)
        self._session.commit()
        self._session.refresh(chunk)
        return chunk

    def complete_chunk(
        self,
        chunk_id: str,
        worker_id: str,
        next_anchor_phash: str | None,
        next_scene_start_ms: int | None,
        scenes: list[dict],
    ) -> bool:
        """
        Complete a chunk: persist scenes, update anchor state on next pending
        chunk, mark chunk complete. All in one transaction.
        Returns False if chunk not found or not owned by worker_id.
        """
        chunk = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)
        ).first()
        if chunk is None or chunk.worker_id != worker_id or chunk.status != "claimed":
            return False

        now = utcnow()

        # Persist scenes
        for s in scenes:
            scene = VideoScene(
                scene_id="scn_" + str(ULID()),
                asset_id=chunk.asset_id,
                scene_index=s["scene_index"],
                start_ms=s["start_ms"],
                end_ms=s["end_ms"],
                rep_frame_ms=s["rep_frame_ms"],
                rep_frame_sha256=s.get("rep_frame_sha256"),
                proxy_key=s.get("proxy_key"),
                thumbnail_key=s.get("thumbnail_key"),
                description=s.get("description"),
                tags=s.get("tags"),
                sharpness_score=s.get("sharpness_score"),
                keep_reason=s.get("keep_reason"),
                phash=s.get("phash"),
                created_at=now,
            )
            self._session.add(scene)

        # Update anchor state on the next pending chunk
        if next_anchor_phash is not None:
            next_chunk = self._session.exec(
                select(VideoIndexChunk).where(
                    VideoIndexChunk.asset_id == chunk.asset_id,
                    VideoIndexChunk.chunk_index == chunk.chunk_index + 1,
                )
            ).first()
            if next_chunk:
                next_chunk.anchor_phash = next_anchor_phash
                next_chunk.scene_start_ms = next_scene_start_ms
                self._session.add(next_chunk)

        chunk.status = "completed"
        chunk.completed_at = now
        self._session.add(chunk)
        self._session.commit()
        return True

    def fail_chunk(self, chunk_id: str, worker_id: str, error_message: str) -> bool:
        """Mark a chunk as failed."""
        chunk = self._session.exec(
            select(VideoIndexChunk).where(VideoIndexChunk.chunk_id == chunk_id)
        ).first()
        if chunk is None or chunk.worker_id != worker_id:
            return False
        chunk.status = "failed"
        chunk.error_message = error_message
        self._session.add(chunk)
        self._session.commit()
        return True

    def delete_for_library(self, library_id: str) -> int:
        """Delete all chunks for all video assets in a library. Returns count deleted."""
        from sqlalchemy import delete as sa_delete
        result = self._session.exec(  # type: ignore[call-overload]
            sa_delete(VideoIndexChunk).where(
                VideoIndexChunk.asset_id.in_(  # type: ignore[attr-defined]
                    select(Asset.asset_id).where(
                        Asset.library_id == library_id,
                        Asset.media_type == "video",
                    )
                )
            )
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]

    def all_chunks_complete(self, asset_id: str) -> bool:
        """True if every chunk for this asset is completed."""
        incomplete = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status != "completed",
            )
        ).first()
        return incomplete is None

    def chunk_count(self, asset_id: str) -> int:
        result = self._session.execute(
            select(func.count()).select_from(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id
            )
        )
        return int(result.scalar() or 0)
