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
    AssetRating,
    Collection,
    CollectionAsset,
    Face,
    FacePersonMatch,
    Library,
    Person,
    LibraryPathFilter,
    TenantPathFilterDefault,
    SavedView,
    VALID_COLORS,
    VideoIndexChunk,
    VideoScene,
)
from ulid import ULID

# Canonical view for non-trashed assets. Use in raw SQL (e.g. FROM active_assets).
ACTIVE_ASSETS = "active_assets"

# Single source of truth for "missing pipeline output" SQL conditions.
# Used by both repair-summary (counting) and page endpoints (filtering).
# All image-only filters include the media_type check.
MISSING_CONDITIONS = {
    "missing_vision": (
        "NOT EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)"
        " AND a.media_type = 'image'"
    ),
    "missing_embeddings": (
        "NOT EXISTS (SELECT 1 FROM asset_embeddings ae WHERE ae.asset_id = a.asset_id)"
        " AND a.media_type = 'image'"
    ),
    "missing_faces": "a.face_count IS NULL AND a.media_type = 'image'",
    "missing_video_scenes": "a.video_indexed = false AND a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    "missing_ocr": (
        "EXISTS (SELECT 1 FROM asset_metadata am WHERE am.asset_id = a.asset_id)"
        " AND NOT EXISTS (SELECT 1 FROM asset_metadata am2 WHERE am2.asset_id = a.asset_id AND am2.data->>'ocr_text' IS NOT NULL AND am2.data->>'ocr_text' != '')"
        " AND a.media_type = 'image'"
    ),
    "missing_scene_vision": (
        "a.video_indexed = true AND a.media_type = 'video'"
        " AND EXISTS (SELECT 1 FROM video_scenes vs WHERE vs.asset_id = a.asset_id AND vs.description IS NULL)"
    ),
}


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
        """Set library status to trashed, soft-delete all its assets, return updated library."""
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
        missing_faces: bool = False,
        missing_video_scenes: bool = False,
        missing_ocr: bool = False,
        missing_scene_vision: bool = False,
        has_faces: bool | None = None,
        person_id: str | None = None,
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
        rating_user_id: str | None = None,
        favorite: bool | None = None,
        star_min: int | None = None,
        star_max: int | None = None,
        color: list[str] | None = None,
        has_rating: bool | None = None,
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

        # --- Tag / missing filters (use shared MISSING_CONDITIONS) ---
        if tag is not None:
            conditions.append("m.tags @> jsonb_build_array(:tag)")
            params["tag"] = tag
        if missing_vision:
            conditions.append(MISSING_CONDITIONS["missing_vision"])
        if missing_embeddings:
            conditions.append(MISSING_CONDITIONS["missing_embeddings"])
        if missing_faces:
            conditions.append(MISSING_CONDITIONS["missing_faces"])
        if missing_video_scenes:
            conditions.append(MISSING_CONDITIONS["missing_video_scenes"])
        if missing_ocr:
            conditions.append(MISSING_CONDITIONS["missing_ocr"])
        if missing_scene_vision:
            conditions.append(MISSING_CONDITIONS["missing_scene_vision"])
        if has_faces is True:
            conditions.append("a.face_count > 0")
        elif has_faces is False:
            conditions.append("(a.face_count IS NULL OR a.face_count = 0)")
        if person_id:
            conditions.append("a.asset_id IN (SELECT asset_id FROM faces WHERE person_id = :person_id)")
            params["person_id"] = person_id

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

        # --- Rating filters (LEFT JOIN on asset_ratings) ---
        join_ratings = (
            rating_user_id is not None
            and (favorite is not None or star_min is not None or star_max is not None or color is not None or has_rating is not None)
        )
        if join_ratings:
            params["rating_user_id"] = rating_user_id
            if favorite is True:
                conditions.append("r.favorite = TRUE")
            elif favorite is False:
                conditions.append("(r.favorite IS NULL OR r.favorite = FALSE)")
            if star_min is not None:
                conditions.append("COALESCE(r.stars, 0) >= :star_min")
                params["star_min"] = star_min
            if star_max is not None:
                conditions.append("COALESCE(r.stars, 0) <= :star_max")
                params["star_max"] = star_max
            if color is not None and len(color) > 0:
                placeholders = ", ".join(f":color_{i}" for i in range(len(color)))
                conditions.append(f"r.color IN ({placeholders})")
                for i, c in enumerate(color):
                    params[f"color_{i}"] = c
            if has_rating is True:
                conditions.append("r.user_id IS NOT NULL")
            elif has_rating is False:
                conditions.append("r.user_id IS NULL")

        # --- Build query ---
        # Lateral join only needed for tag filtering (m.tags reference).
        # missing_vision/embeddings/faces use self-contained subqueries.
        join_metadata = tag is not None
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

        rating_join = ""
        if join_ratings:
            rating_join = """
            LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id AND r.user_id = :rating_user_id
            """

        if sort_col == "asset_id":
            order_clause = f"a.asset_id {order_dir}"
        else:
            order_clause = f"a.{sort_col} {order_dir} NULLS LAST, a.asset_id {order_dir}"

        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            {lateral_join}
            {rating_join}
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


# ---------------------------------------------------------------------------
# Collections (ADR-006)
# ---------------------------------------------------------------------------

_SENTINEL = object()  # distinguishes "not provided" from None


class CollectionRepository:
    """Repository for collections and collection_assets tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- Collection CRUD ----

    def create(
        self,
        name: str,
        owner_user_id: str | None = None,
        description: str | None = None,
        sort_order: str = "manual",
        visibility: str = "private",
    ) -> Collection:
        collection_id = "col_" + str(ULID())
        collection = Collection(
            collection_id=collection_id,
            name=name,
            owner_user_id=owner_user_id,
            description=description,
            sort_order=sort_order,
            visibility=visibility,
        )
        self._session.add(collection)
        self._session.commit()
        self._session.refresh(collection)
        return collection

    def get_by_id(self, collection_id: str) -> Collection | None:
        return self._session.exec(
            select(Collection).where(Collection.collection_id == collection_id)
        ).first()

    def list_for_user(self, user_id: str) -> list[Collection]:
        """Return collections owned by user + shared collections."""
        return list(
            self._session.exec(
                select(Collection)
                .where(
                    or_(
                        Collection.owner_user_id == user_id,
                        Collection.owner_user_id.is_(None),  # type: ignore[union-attr]
                        Collection.visibility.in_(["shared", "public"]),  # type: ignore[union-attr]
                    )
                )
                .order_by(Collection.created_at.desc())  # type: ignore[attr-defined]
            ).all()
        )

    def update(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        description: str | None = _SENTINEL,
        visibility: str | None = None,
        sort_order: str | None = None,
        cover_asset_id: str | None = _SENTINEL,
    ) -> Collection | None:
        col = self.get_by_id(collection_id)
        if col is None:
            return None
        if name is not None:
            col.name = name
        if description is not _SENTINEL:
            col.description = description
        if visibility is not None:
            col.visibility = visibility
        if sort_order is not None:
            col.sort_order = sort_order
        if cover_asset_id is not _SENTINEL:
            col.cover_asset_id = cover_asset_id
        col.updated_at = utcnow()
        self._session.add(col)
        self._session.commit()
        self._session.refresh(col)
        return col

    def delete(self, collection_id: str) -> bool:
        col = self.get_by_id(collection_id)
        if col is None:
            return False
        self._session.delete(col)
        self._session.commit()
        return True

    # ---- Asset count (no denormalized column) ----

    def asset_count(self, collection_id: str) -> int:
        result = self._session.execute(
            select(func.count())
            .select_from(CollectionAsset)
            .join(Asset, CollectionAsset.asset_id == Asset.asset_id)
            .where(
                CollectionAsset.collection_id == collection_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
        return int(result.scalar() or 0)

    # ---- Batch add / remove ----

    def add_assets(self, collection_id: str, asset_ids: list[str]) -> int:
        """Add assets to collection. Returns count actually inserted (idempotent)."""
        if not asset_ids:
            return 0

        # Get current max position
        max_pos_result = self._session.execute(
            select(func.max(CollectionAsset.position)).where(
                CollectionAsset.collection_id == collection_id
            )
        )
        next_pos = (max_pos_result.scalar() or -1) + 1

        inserted = 0
        for asset_id in asset_ids:
            stmt = pg_insert(CollectionAsset).values(
                collection_id=collection_id,
                asset_id=asset_id,
                position=next_pos,
                added_at=utcnow(),
            ).on_conflict_do_nothing(index_elements=["collection_id", "asset_id"])
            result = self._session.execute(stmt)
            if result.rowcount:  # type: ignore[union-attr]
                inserted += 1
                next_pos += 1
        self._session.commit()
        return inserted

    def remove_assets(self, collection_id: str, asset_ids: list[str]) -> int:
        """Remove assets from collection. Returns count removed."""
        if not asset_ids:
            return 0
        from sqlalchemy import delete as sa_delete

        result = self._session.execute(
            sa_delete(CollectionAsset).where(
                CollectionAsset.collection_id == collection_id,
                CollectionAsset.asset_id.in_(asset_ids),  # type: ignore[attr-defined]
            )
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]

    # ---- List assets (paginated, ordered) ----

    def list_assets(
        self,
        collection_id: str,
        sort_order: str = "manual",
        after_cursor: str | None = None,
        limit: int = 200,
    ) -> tuple[list[Asset], str | None]:
        """Return active assets in collection with cursor pagination.

        Returns (assets, next_cursor). Cursor is the position/added_at/taken_at value
        of the last returned row, encoded as a string.
        """
        import base64 as _b64

        query = (
            select(Asset, CollectionAsset.position, CollectionAsset.added_at)
            .join(CollectionAsset, CollectionAsset.asset_id == Asset.asset_id)
            .where(
                CollectionAsset.collection_id == collection_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )

        if sort_order == "added_at":
            order_col = CollectionAsset.added_at
        elif sort_order == "taken_at":
            order_col = Asset.taken_at
        else:  # manual
            order_col = CollectionAsset.position

        query = query.order_by(order_col.asc(), Asset.asset_id.asc())  # type: ignore[union-attr]

        if after_cursor:
            try:
                padded = after_cursor + "=" * (-len(after_cursor) % 4)
                decoded = json.loads(_b64.urlsafe_b64decode(padded))
                cursor_val = decoded["v"]
                cursor_id = decoded["id"]
                query = query.where(
                    or_(
                        order_col > cursor_val,  # type: ignore[operator]
                        and_(order_col == cursor_val, Asset.asset_id > cursor_id),  # type: ignore[operator]
                    )
                )
            except Exception:
                pass  # ignore bad cursors

        rows = self._session.execute(query.limit(limit + 1)).all()

        assets: list[Asset] = []
        next_cursor: str | None = None
        for i, row in enumerate(rows):
            if i >= limit:
                # Encode cursor from last returned row
                last_asset = assets[-1]
                last_row = rows[i - 1]
                cursor_payload = json.dumps(
                    {"v": str(last_row[1] if sort_order == "manual" else last_row[2]), "id": last_asset.asset_id},
                    default=str,
                )
                next_cursor = _b64.urlsafe_b64encode(cursor_payload.encode()).decode().rstrip("=")
                break
            assets.append(row[0])

        return assets, next_cursor

    # ---- Reorder ----

    def reorder(self, collection_id: str, asset_ids: list[str]) -> bool:
        """Reorder assets in collection. asset_ids must include ALL active assets.

        Returns True on success. Raises ValueError if list is incomplete/has extras.
        """
        # Get current active asset IDs in collection
        rows = self._session.execute(
            select(CollectionAsset.asset_id)
            .join(Asset, CollectionAsset.asset_id == Asset.asset_id)
            .where(
                CollectionAsset.collection_id == collection_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).all()
        current_ids = {r[0] for r in rows}
        submitted_ids = set(asset_ids)

        if current_ids != submitted_ids:
            raise ValueError(
                f"Submitted {len(submitted_ids)} asset IDs but collection has {len(current_ids)} active assets. "
                "Reorder must include all active assets in the collection."
            )

        for position, asset_id in enumerate(asset_ids):
            self._session.execute(
                sa_text(
                    "UPDATE collection_assets SET position = :pos "
                    "WHERE collection_id = :cid AND asset_id = :aid"
                ),
                {"pos": position, "cid": collection_id, "aid": asset_id},
            )
        self._session.commit()
        return True

    # ---- Cover resolution ----

    def resolve_cover(self, collection: Collection) -> str | None:
        """Return the effective cover asset_id, applying lazy self-healing.

        If cover_asset_id is set and the asset is active and in the collection,
        return it. Otherwise fall back to first-by-position, and null out the
        stale cover_asset_id.
        """
        if collection.cover_asset_id:
            # Check if cover asset is still active and in collection
            row = self._session.execute(
                select(CollectionAsset.asset_id)
                .join(Asset, CollectionAsset.asset_id == Asset.asset_id)
                .where(
                    CollectionAsset.collection_id == collection.collection_id,
                    CollectionAsset.asset_id == collection.cover_asset_id,
                    Asset.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            ).first()
            if row:
                return collection.cover_asset_id

            # Stale — null it out (lazy self-healing)
            collection.cover_asset_id = None
            collection.updated_at = utcnow()
            self._session.add(collection)
            self._session.commit()

        # Fallback: first active asset by position
        row = self._session.execute(
            select(CollectionAsset.asset_id)
            .join(Asset, CollectionAsset.asset_id == Asset.asset_id)
            .where(
                CollectionAsset.collection_id == collection.collection_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(CollectionAsset.position.asc())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Ratings (ADR-007)
# ---------------------------------------------------------------------------


class RatingRepository:
    """Repository for user-scoped asset ratings (favorites, stars, color labels)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        user_id: str,
        asset_id: str,
        *,
        favorite: bool | None = None,
        stars: int | None = None,
        color: str | object = _SENTINEL,
    ) -> AssetRating | None:
        """Set or update a rating. Only provided fields are changed.

        Returns the rating, or None if the row was deleted (all-default state).
        """
        existing = self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id == asset_id,
            )
        ).first()

        if existing:
            if favorite is not None:
                existing.favorite = favorite
            if stars is not None:
                existing.stars = stars
            if color is not _SENTINEL:
                existing.color = color  # type: ignore[assignment]
            existing.updated_at = utcnow()

            # Delete row if all-default
            if not existing.favorite and existing.stars == 0 and existing.color is None:
                self._session.delete(existing)
                self._session.commit()
                return None

            self._session.add(existing)
            self._session.commit()
            self._session.refresh(existing)
            return existing

        # New row — apply defaults for unprovided fields
        fav = favorite if favorite is not None else False
        st = stars if stars is not None else 0
        col = color if color is not _SENTINEL else None

        # Don't insert if all-default
        if not fav and st == 0 and col is None:
            return None

        rating = AssetRating(
            user_id=user_id,
            asset_id=asset_id,
            favorite=fav,
            stars=st,
            color=col,  # type: ignore[arg-type]
            updated_at=utcnow(),
        )
        self._session.add(rating)
        self._session.commit()
        self._session.refresh(rating)
        return rating

    def get_for_asset(self, user_id: str, asset_id: str) -> AssetRating | None:
        return self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id == asset_id,
            )
        ).first()

    def get_for_assets(
        self, user_id: str, asset_ids: list[str]
    ) -> dict[str, AssetRating]:
        """Bulk read — returns dict keyed by asset_id."""
        if not asset_ids:
            return {}
        rows = self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id.in_(asset_ids),  # type: ignore[attr-defined]
            )
        ).all()
        return {r.asset_id: r for r in rows}

    def batch_upsert(
        self,
        user_id: str,
        asset_ids: list[str],
        *,
        favorite: bool | None = None,
        stars: int | None = None,
        color: str | object = _SENTINEL,
    ) -> int:
        """Apply the same rating update to multiple assets. Returns count updated."""
        if not asset_ids:
            return 0

        updated = 0
        for asset_id in asset_ids:
            self.upsert(
                user_id, asset_id, favorite=favorite, stars=stars, color=color
            )
            updated += 1
        return updated

    def list_favorites(
        self, user_id: str, after: str | None = None, limit: int = 200
    ) -> tuple[list[Asset], str | None]:
        """Return active favorited assets across all libraries, newest first.

        Uses cursor pagination on asset_ratings.updated_at DESC.
        Returns (assets, next_cursor).
        """
        conditions = [
            "r.user_id = :user_id",
            "r.favorite = TRUE",
            "a.deleted_at IS NULL",
        ]
        params: dict = {"user_id": user_id, "limit": limit + 1}

        if after:
            conditions.append("r.updated_at < :after_ts")
            from datetime import datetime as _dt, timezone as _tz
            params["after_ts"] = _dt.fromisoformat(after.replace("Z", "+00:00"))

        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT a.asset_id, r.updated_at
            FROM asset_ratings r
            JOIN assets a ON a.asset_id = r.asset_id
            WHERE {where_sql}
            ORDER BY r.updated_at DESC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()

        asset_ids = [row[0] for row in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit:
            next_cursor = rows[limit - 1][1].isoformat()

        if not asset_ids:
            return [], None

        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        assets_by_id = {a.asset_id: a for a in self._session.exec(stmt).all()}
        ordered = [assets_by_id[aid] for aid in asset_ids if aid in assets_by_id]
        return ordered, next_cursor

    def delete_for_user(self, user_id: str) -> int:
        """Delete all ratings for a user. Used when a user account is deleted."""
        from sqlalchemy import delete as sa_delete

        result = self._session.execute(
            sa_delete(AssetRating).where(AssetRating.user_id == user_id)
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]


class UnifiedBrowseRepository:
    """Cross-library browse — queries active_assets without library_id constraint."""

    SORTABLE_COLUMNS = {
        "asset_id", "taken_at", "created_at", "file_size",
        "iso", "aperture", "focal_length", "rel_path",
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    def page(
        self,
        after: str | None,
        limit: int,
        library_ids: list[str] | None = None,
        path_prefix: str | None = None,
        tag: str | None = None,
        missing_vision: bool = False,
        missing_embeddings: bool = False,
        missing_faces: bool = False,
        missing_video_scenes: bool = False,
        missing_scene_vision: bool = False,
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
        rating_user_id: str | None = None,
        favorite: bool | None = None,
        star_min: int | None = None,
        star_max: int | None = None,
        color: list[str] | None = None,
        has_rating: bool | None = None,
        has_faces: bool | None = None,
        person_id: str | None = None,
    ) -> list[Asset]:
        """Keyset pagination across all libraries. Same filter support as page_by_library."""
        import base64 as _b64
        import math as _math

        sort_col = sort if sort in self.SORTABLE_COLUMNS else "taken_at"
        is_desc = direction.lower() == "desc"
        cmp_op = "<" if is_desc else ">"
        order_dir = "DESC" if is_desc else "ASC"

        conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}

        # --- Library filter ---
        if library_ids:
            conditions.append("a.library_id = ANY(:library_ids)")
            params["library_ids"] = library_ids

        # --- Path prefix (requires library_ids) ---
        if path_prefix:
            conditions.append(
                "(a.rel_path = :path_prefix OR a.rel_path LIKE :path_prefix_like)"
            )
            params["path_prefix"] = path_prefix
            params["path_prefix_like"] = path_prefix + "/%"

        # --- Composite cursor ---
        if after is not None:
            cursor_value = None
            cursor_id = after
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_value = decoded["v"]
                cursor_id = decoded["id"]
            except Exception:
                if sort_col != "asset_id":
                    sort_col = "asset_id"

            if sort_col == "asset_id":
                conditions.append(f"a.asset_id {cmp_op} :cursor_id")
                params["cursor_id"] = cursor_id
            else:
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

        # --- Tag / missing filters (use shared MISSING_CONDITIONS) ---
        if tag is not None:
            conditions.append("m.tags @> jsonb_build_array(:tag)")
            params["tag"] = tag
        if missing_vision:
            conditions.append(MISSING_CONDITIONS["missing_vision"])
        if missing_embeddings:
            conditions.append(MISSING_CONDITIONS["missing_embeddings"])
        if missing_faces:
            conditions.append(MISSING_CONDITIONS["missing_faces"])
        if missing_video_scenes:
            conditions.append(MISSING_CONDITIONS["missing_video_scenes"])
        if missing_ocr:
            conditions.append(MISSING_CONDITIONS["missing_ocr"])
        if missing_scene_vision:
            conditions.append(MISSING_CONDITIONS["missing_scene_vision"])
        if has_faces is True:
            conditions.append("a.face_count > 0")
        elif has_faces is False:
            conditions.append("(a.face_count IS NULL OR a.face_count = 0)")
        if person_id:
            conditions.append("a.asset_id IN (SELECT asset_id FROM faces WHERE person_id = :person_id)")
            params["person_id"] = person_id

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

        # --- Rating filters (LEFT JOIN on asset_ratings) ---
        join_ratings = (
            rating_user_id is not None
            and (favorite is not None or star_min is not None or star_max is not None or color is not None or has_rating is not None)
        )
        if join_ratings:
            params["rating_user_id"] = rating_user_id
            if favorite is True:
                conditions.append("r.favorite = TRUE")
            elif favorite is False:
                conditions.append("(r.favorite IS NULL OR r.favorite = FALSE)")
            if star_min is not None:
                conditions.append("COALESCE(r.stars, 0) >= :star_min")
                params["star_min"] = star_min
            if star_max is not None:
                conditions.append("COALESCE(r.stars, 0) <= :star_max")
                params["star_max"] = star_max
            if color is not None and len(color) > 0:
                placeholders = ", ".join(f":color_{i}" for i in range(len(color)))
                conditions.append(f"r.color IN ({placeholders})")
                for i, c in enumerate(color):
                    params[f"color_{i}"] = c
            if has_rating is True:
                conditions.append("r.user_id IS NOT NULL")
            elif has_rating is False:
                conditions.append("r.user_id IS NULL")

        # --- Build query ---
        join_metadata = tag is not None
        where_sql = " AND ".join(conditions) if conditions else "TRUE"

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

        rating_join = ""
        if join_ratings:
            rating_join = """
            LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id AND r.user_id = :rating_user_id
            """

        if sort_col == "asset_id":
            order_clause = f"a.asset_id {order_dir}"
        else:
            order_clause = f"a.{sort_col} {order_dir} NULLS LAST, a.asset_id {order_dir}"

        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            {lateral_join}
            {rating_join}
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


class SavedViewRepository:
    """CRUD for saved views (bookmarked filter presets)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, owner_user_id: str, name: str, query_params: str, icon: str | None = None) -> SavedView:
        max_pos = self._session.execute(
            text("SELECT COALESCE(MAX(position), -1) FROM saved_views WHERE owner_user_id = :uid"),
            {"uid": owner_user_id},
        ).scalar()
        view = SavedView(
            view_id=f"sv_{ULID()}",
            name=name,
            query_params=query_params,
            icon=icon,
            owner_user_id=owner_user_id,
            position=(max_pos or 0) + 1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self._session.add(view)
        self._session.commit()
        self._session.refresh(view)
        return view

    def list_for_user(self, owner_user_id: str) -> list[SavedView]:
        stmt = (
            select(SavedView)
            .where(SavedView.owner_user_id == owner_user_id)
            .order_by(SavedView.position)
        )
        return list(self._session.exec(stmt).all())

    def get(self, view_id: str) -> SavedView | None:
        return self._session.get(SavedView, view_id)

    def update(self, view: SavedView, name: str | None = None, query_params: str | None = None, icon: object = _SENTINEL) -> SavedView:
        if name is not None:
            view.name = name
        if query_params is not None:
            view.query_params = query_params
        if icon is not _SENTINEL:
            view.icon = icon  # type: ignore[assignment]
        view.updated_at = utcnow()
        self._session.add(view)
        self._session.commit()
        self._session.refresh(view)
        return view

    def delete(self, view: SavedView) -> None:
        self._session.delete(view)
        self._session.commit()

    def reorder(self, owner_user_id: str, view_ids: list[str]) -> None:
        for i, vid in enumerate(view_ids):
            self._session.execute(
                text("UPDATE saved_views SET position = :pos, updated_at = :now WHERE view_id = :vid AND owner_user_id = :uid"),
                {"pos": i, "now": utcnow(), "vid": vid, "uid": owner_user_id},
            )
        self._session.commit()

    def delete_for_user(self, user_id: str) -> int:
        from sqlalchemy import delete as sa_delete
        result = self._session.execute(
            sa_delete(SavedView).where(SavedView.owner_user_id == user_id)
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]


def _mark_clusters_dirty(session: Session) -> None:
    """Mark face cluster cache as stale. Called after any face mutation."""
    session.execute(
        text("""
            INSERT INTO system_metadata (key, value, updated_at)
            VALUES ('face_clusters_dirty', 'true', NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = 'true', updated_at = NOW()
        """)
    )


class FaceRepository:
    """CRUD for detected faces. Operates within a tenant session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def submit_faces(
        self,
        asset_id: str,
        detection_model: str,
        detection_model_version: str,
        faces: list[dict],
    ) -> list[str]:
        """Replace all faces for (asset_id, model, version) and update face_count.

        Args:
            faces: list of dicts with keys: bounding_box, detection_confidence, embedding (optional).

        Returns:
            List of created face_ids.
        """
        from sqlalchemy import delete as sa_delete

        # Delete existing faces for this asset + model combo
        self._session.execute(
            sa_delete(Face).where(
                Face.asset_id == asset_id,
                Face.detection_model == detection_model,
                Face.detection_model_version == detection_model_version,
            )
        )

        face_ids: list[str] = []
        for f in faces:
            face_id = "face_" + str(ULID())
            face = Face(
                face_id=face_id,
                asset_id=asset_id,
                bounding_box_json=f.get("bounding_box"),
                embedding_vector=f.get("embedding"),
                detection_confidence=f.get("detection_confidence"),
                detection_model=detection_model,
                detection_model_version=detection_model_version,
            )
            self._session.add(face)
            face_ids.append(face_id)

        # Update face_count on the asset
        self._session.execute(
            text("UPDATE assets SET face_count = :count WHERE asset_id = :aid"),
            {"count": len(faces), "aid": asset_id},
        )

        # Auto-assign faces to known people by centroid proximity
        self._auto_assign_by_centroid(face_ids, faces)

        _mark_clusters_dirty(self._session)
        self._session.commit()
        return face_ids

    # Auto-assign threshold — tighter than clustering (0.55) because centroids
    # are averaged over many confirmed faces and more stable.
    AUTO_ASSIGN_THRESHOLD = 0.45

    def _auto_assign_by_centroid(self, face_ids: list[str], faces_data: list[dict]) -> None:
        """Auto-assign new faces to known people if embedding is close to a centroid.

        Only assigns faces that have embeddings. Uses cosine distance against
        person centroid vectors. Assigned with confirmed=false so user can review.
        """
        # Collect faces with embeddings
        faces_with_emb = [
            (fid, fd["embedding"])
            for fid, fd in zip(face_ids, faces_data)
            if fd.get("embedding")
        ]
        if not faces_with_emb:
            return

        # Get all people with centroids
        rows = self._session.execute(
            text("SELECT person_id, centroid_vector::text FROM people WHERE centroid_vector IS NOT NULL")
        ).all()
        if not rows:
            return

        import numpy as np

        # Parse centroids
        person_ids = [r[0] for r in rows]
        centroids = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in rows],
            dtype=np.float32,
        )
        # L2-normalize centroids
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        centroids = centroids / norms

        for face_id, embedding in faces_with_emb:
            vec = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            # Cosine distance to each centroid
            distances = 1.0 - (centroids @ vec)
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])

            if best_dist < self.AUTO_ASSIGN_THRESHOLD:
                best_person_id = person_ids[best_idx]
                # Create match (confirmed=false for auto-assignment)
                match_id = "fpm_" + str(ULID())
                self._session.add(FacePersonMatch(
                    match_id=match_id,
                    face_id=face_id,
                    person_id=best_person_id,
                    confidence=1.0 - best_dist,
                    confirmed=False,
                ))
                # Sync denormalized column
                self._session.execute(
                    text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
                    {"pid": best_person_id, "fid": face_id},
                )
        self._session.flush()

    def propagate_assignments(self, batch_size: int = 5000) -> dict:
        """Scan unassigned faces and auto-assign to known people by centroid proximity.

        Returns {"assigned": N, "scanned": N}.
        Called by the upkeep timer to continuously improve tagging as users
        manually assign faces and centroids shift.
        """
        import numpy as np

        # Get all people with centroids
        people_rows = self._session.execute(
            text("SELECT person_id, centroid_vector::text FROM people WHERE centroid_vector IS NOT NULL")
        ).all()
        if not people_rows:
            return {"assigned": 0, "scanned": 0}

        person_ids = [r[0] for r in people_rows]
        centroids = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in people_rows],
            dtype=np.float32,
        )
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        centroids = centroids / norms

        # Get unassigned faces with embeddings
        rows = self._session.execute(
            text("""
                SELECT f.face_id, f.embedding_vector::text
                FROM faces f
                LEFT JOIN face_person_matches m ON m.face_id = f.face_id
                WHERE m.match_id IS NULL AND f.embedding_vector IS NOT NULL
                ORDER BY f.detection_confidence DESC NULLS LAST
                LIMIT :limit
            """),
            {"limit": batch_size},
        ).all()

        if not rows:
            return {"assigned": 0, "scanned": 0}

        assigned = 0
        for face_id, emb_text in rows:
            vec = np.array([float(x) for x in emb_text.strip("[]").split(",")], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            distances = 1.0 - (centroids @ vec)
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])

            if best_dist < self.AUTO_ASSIGN_THRESHOLD:
                best_person_id = person_ids[best_idx]
                match_id = "fpm_" + str(ULID())
                self._session.add(FacePersonMatch(
                    match_id=match_id,
                    face_id=face_id,
                    person_id=best_person_id,
                    confidence=1.0 - best_dist,
                    confirmed=False,
                ))
                self._session.execute(
                    text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
                    {"pid": best_person_id, "fid": face_id},
                )
                assigned += 1

        if assigned > 0:
            _mark_clusters_dirty(self._session)
            self._session.commit()

        return {"assigned": assigned, "scanned": len(rows)}

    def get_by_asset_id(self, asset_id: str) -> list[Face]:
        """Return all faces for an asset, ordered by confidence desc."""
        stmt = (
            select(Face)
            .where(Face.asset_id == asset_id)
            .order_by(Face.detection_confidence.desc())  # type: ignore[union-attr]
        )
        return list(self._session.exec(stmt).all())

    def get_person_for_face(self, face_id: str) -> Person | None:
        """Return the person matched to a face, or None."""
        stmt = (
            select(Person)
            .join(FacePersonMatch, FacePersonMatch.person_id == Person.person_id)
            .where(FacePersonMatch.face_id == face_id)
        )
        return self._session.exec(stmt).first()

    def get_persons_for_faces(self, face_ids: list[str]) -> dict[str, Person]:
        """Return {face_id: Person} for all matched faces. Unmatched faces are absent."""
        if not face_ids:
            return {}
        stmt = (
            select(FacePersonMatch.face_id, Person)
            .join(Person, Person.person_id == FacePersonMatch.person_id)
            .where(FacePersonMatch.face_id.in_(face_ids))  # type: ignore[union-attr]
        )
        return {row[0]: row[1] for row in self._session.exec(stmt).all()}


    def compute_clusters(
        self,
        *,
        similarity_threshold: float = 0.55,
        k: int = 10,
        max_faces: int = 5000,
        min_cluster_size: int = 2,
        max_clusters: int = 20,
        faces_per_cluster: int = 6,
    ) -> tuple[list[list[dict]], bool]:
        """Compute clusters of unassigned faces.

        Fetches all unassigned face embeddings in one query, computes the
        cosine distance matrix in numpy, then builds connected components
        via union-find. This avoids N per-face SQL round-trips which caused
        the endpoint to hang (the cross-join query couldn't use HNSW).

        Returns (clusters, truncated) where each cluster is a list of face dicts
        sorted by detection_confidence desc, and clusters are sorted by size desc.
        """
        import numpy as np

        # Get unassigned face IDs + embeddings in one query
        sql = """
            SELECT f.face_id, f.embedding_vector::text
            FROM faces f
            LEFT JOIN face_person_matches m ON m.face_id = f.face_id
            WHERE m.match_id IS NULL AND f.embedding_vector IS NOT NULL
            ORDER BY f.detection_confidence DESC NULLS LAST
            LIMIT :max_faces
        """
        rows = self._session.execute(text(sql).bindparams(max_faces=max_faces)).all()

        truncated = len(rows) == max_faces

        if len(rows) < min_cluster_size:
            return [], truncated

        face_ids = [r[0] for r in rows]
        # Parse pgvector text format "[0.1,0.2,...]" → numpy array
        vectors = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in rows],
            dtype=np.float32,
        )

        # L2-normalize (ArcFace embeddings should already be normalized, belt-and-suspenders)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms

        # Cosine distance matrix: 1 - dot(a, b). Shape: (N, N)
        cosine_sim = vectors @ vectors.T
        dist_matrix = 1.0 - cosine_sim

        # Build adjacency from K nearest neighbors within threshold
        n = len(face_ids)
        parent: dict[int, int] = {i: i for i in range(n)}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            # Get K nearest neighbors (excluding self)
            dists = dist_matrix[i]
            # Set self-distance to infinity so it's excluded
            dists[i] = float("inf")
            nearest_k = np.argpartition(dists, min(k, n - 1))[:k]
            for j in nearest_k:
                if dists[j] < similarity_threshold:
                    union(i, j)

        # Group into clusters
        from collections import defaultdict
        clusters_map: dict[int, list[str]] = defaultdict(list)
        for i in range(n):
            clusters_map[find(i)].append(face_ids[i])

        # Filter by min size, sort by size desc
        clusters = [c for c in clusters_map.values() if len(c) >= min_cluster_size]
        clusters.sort(key=len, reverse=True)
        clusters = clusters[:max_clusters]

        # Load face data for each cluster
        all_face_ids = [fid for c in clusters for fid in c]
        if not all_face_ids:
            return [], truncated

        stmt = select(Face).where(Face.face_id.in_(all_face_ids))  # type: ignore[union-attr]
        faces_by_id = {f.face_id: f for f in self._session.exec(stmt).all()}

        # Also get asset data for thumbnails
        asset_ids = list({faces_by_id[fid].asset_id for fid in all_face_ids if fid in faces_by_id})
        asset_map: dict[str, object] = {}
        if asset_ids:
            asset_stmt = select(Asset).where(Asset.asset_id.in_(asset_ids))  # type: ignore[union-attr]
            asset_map = {a.asset_id: a for a in self._session.exec(asset_stmt).all()}

        result = []
        all_ids_per_cluster = []
        for cluster_face_ids in clusters:
            # Sort by confidence desc within cluster
            cluster_faces = [faces_by_id[fid] for fid in cluster_face_ids if fid in faces_by_id]
            cluster_faces.sort(key=lambda f: f.detection_confidence or 0, reverse=True)

            all_ids_per_cluster.append([f.face_id for f in cluster_faces])

            # Limit to faces_per_cluster samples
            sample = cluster_faces[:faces_per_cluster]
            cluster_data = []
            for f in sample:
                asset = asset_map.get(f.asset_id)
                cluster_data.append({
                    "face_id": f.face_id,
                    "asset_id": f.asset_id,
                    "bounding_box": f.bounding_box_json,
                    "detection_confidence": f.detection_confidence,
                    "rel_path": asset.rel_path if asset else None,
                })
            result.append(cluster_data)

        return result, all_ids_per_cluster, truncated


class PersonRepository:
    """CRUD for people (tenant-scoped). Operates within a tenant session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, display_name: str, *, face_ids: list[str] | None = None) -> Person:
        """Create a new person. Optionally assign faces."""
        person_id = "person_" + str(ULID())
        person = Person(
            person_id=person_id,
            display_name=display_name,
            created_by_user=True,
        )
        self._session.add(person)
        self._session.flush()

        if face_ids:
            self._assign_faces(person_id, face_ids)
            self._recompute_centroid(person_id)
            # Set representative face = highest confidence
            rep = self._session.execute(
                text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST "
                    "LIMIT 1"
                ),
                {"pid": person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep

        if face_ids:
            _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(person)
        return person

    def create_dismissed(self, *, face_ids: list[str]) -> Person:
        """Create a dismissed person from a cluster. Faces are assigned but
        the person is hidden from the UI. Future similar faces are auto-absorbed."""
        person_id = "person_" + str(ULID())
        person = Person(
            person_id=person_id,
            display_name="(dismissed)",
            created_by_user=True,
            dismissed=True,
        )
        self._session.add(person)
        self._session.flush()

        if face_ids:
            self._assign_faces(person_id, face_ids)
            self._recompute_centroid(person_id)
            # Pick highest-confidence face as representative
            rep = self._session.execute(
                text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST "
                    "LIMIT 1"
                ),
                {"pid": person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep

        self._session.commit()
        self._session.refresh(person)
        return person

    def get_by_id(self, person_id: str) -> Person | None:
        return self._session.get(Person, person_id)

    def list_with_face_counts(
        self,
        *,
        after: str | None = None,
        limit: int = 50,
        q: str | None = None,
    ) -> list[tuple[Person, int]]:
        """Return people with face counts, sorted by face count desc.

        Cursor is base64-encoded JSON {"count": N, "id": person_id}.
        Optional q filters by display_name (case-insensitive substring match).
        Returns list of (Person, face_count) tuples.
        """
        import base64 as _b64

        having_conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}

        if q:
            having_conditions.append("LOWER(p.display_name) LIKE '%' || LOWER(:q) || '%'")
            params["q"] = q

        if after:
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_count = decoded["count"]
                cursor_id = decoded["id"]
                having_conditions.append(
                    "(COUNT(m.match_id)::int < :cursor_count OR (COUNT(m.match_id)::int = :cursor_count AND p.person_id > :cursor_id))"
                )
                params["cursor_count"] = cursor_count
                params["cursor_id"] = cursor_id
            except Exception:
                pass

        having_sql = (" HAVING " + " AND ".join(having_conditions)) if having_conditions else ""

        sql = f"""
            SELECT p.person_id, p.display_name, p.created_by_user,
                   p.representative_face_id, p.confirmation_count, p.created_at,
                   COUNT(m.match_id)::int AS cnt
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            WHERE p.dismissed = false
            GROUP BY p.person_id
            {having_sql}
            ORDER BY cnt DESC, p.person_id ASC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()
        result = []
        for row in rows:
            person = self.get_by_id(row.person_id)
            if person:
                result.append((person, row.cnt))
        return result

    def list_dismissed(
        self,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[tuple[Person, int]]:
        """Return dismissed people with face counts, sorted by face count desc."""
        import base64 as _b64

        having_conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}

        if after:
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_count = decoded["count"]
                cursor_id = decoded["id"]
                having_conditions.append(
                    "(COUNT(m.match_id)::int < :cursor_count OR (COUNT(m.match_id)::int = :cursor_count AND p.person_id > :cursor_id))"
                )
                params["cursor_count"] = cursor_count
                params["cursor_id"] = cursor_id
            except Exception:
                pass

        having_sql = (" HAVING " + " AND ".join(having_conditions)) if having_conditions else ""

        sql = f"""
            SELECT p.person_id, p.display_name, p.created_by_user,
                   p.representative_face_id, p.confirmation_count, p.created_at,
                   COUNT(m.match_id)::int AS cnt
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            WHERE p.dismissed = true
            GROUP BY p.person_id
            {having_sql}
            ORDER BY cnt DESC, p.person_id ASC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()
        result = []
        for row in rows:
            person = self.get_by_id(row.person_id)
            if person:
                result.append((person, row.cnt))
        return result

    def get_face_count(self, person_id: str) -> int:
        """Return the number of faces matched to a person."""
        result = self._session.execute(
            text("SELECT COUNT(*)::int FROM face_person_matches WHERE person_id = :pid"),
            {"pid": person_id},
        ).scalar()
        return result or 0

    def update_name(self, person_id: str, display_name: str) -> Person | None:
        person = self.get_by_id(person_id)
        if person is None:
            return None
        person.display_name = display_name
        self._session.add(person)
        self._session.commit()
        self._session.refresh(person)
        return person

    def delete(self, person_id: str) -> bool:
        """Delete a person and all their face matches."""
        person = self.get_by_id(person_id)
        if person is None:
            return False
        # Clear denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = NULL WHERE person_id = :pid"),
            {"pid": person_id},
        )
        # Delete matches
        self._session.execute(
            text("DELETE FROM face_person_matches WHERE person_id = :pid"),
            {"pid": person_id},
        )
        self._session.delete(person)
        _mark_clusters_dirty(self._session)
        self._session.commit()
        return True

    def get_faces(
        self,
        person_id: str,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Face]:
        """Return faces matched to a person, cursor-paginated by face_id."""
        conditions = ["m.person_id = :pid"]
        params: dict[str, object] = {"pid": person_id, "limit": limit}

        if after:
            conditions.append("f.face_id > :after")
            params["after"] = after

        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT f.face_id
            FROM faces f
            JOIN face_person_matches m ON m.face_id = f.face_id
            WHERE {where_sql}
            ORDER BY f.face_id ASC
            LIMIT :limit
        """
        face_ids = [row[0] for row in self._session.execute(text(sql).bindparams(**params)).all()]
        if not face_ids:
            return []
        stmt = select(Face).where(Face.face_id.in_(face_ids))  # type: ignore[union-attr]
        faces_by_id = {f.face_id: f for f in self._session.exec(stmt).all()}
        return [faces_by_id[fid] for fid in face_ids if fid in faces_by_id]

    def assign_face(self, face_id: str, person_id: str, *, confidence: float | None = None, confirmed: bool = False) -> FacePersonMatch:
        """Assign a face to a person. Raises if face is already assigned (unique constraint)."""
        match_id = "fpm_" + str(ULID())
        match = FacePersonMatch(
            match_id=match_id,
            face_id=face_id,
            person_id=person_id,
            confidence=confidence,
            confirmed=confirmed,
            confirmed_at=utcnow() if confirmed else None,
        )
        self._session.add(match)
        # Sync denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
            {"pid": person_id, "fid": face_id},
        )
        self._recompute_centroid(person_id)
        _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(match)
        return match

    def unassign_face(self, face_id: str) -> bool:
        """Remove face-person assignment and clear denormalized faces.person_id."""
        # Get the person_id before deleting (for centroid recomputation)
        old_pid = self._session.execute(
            text("SELECT person_id FROM face_person_matches WHERE face_id = :fid"),
            {"fid": face_id},
        ).scalar()
        result = self._session.execute(
            text("DELETE FROM face_person_matches WHERE face_id = :fid"),
            {"fid": face_id},
        )
        if result.rowcount > 0:  # type: ignore[union-attr]
            self._session.execute(
                text("UPDATE faces SET person_id = NULL WHERE face_id = :fid"),
                {"fid": face_id},
            )
            if old_pid:
                self._recompute_centroid(old_pid)
            _mark_clusters_dirty(self._session)
            self._session.commit()
            return True
        return False

    def _assign_faces(self, person_id: str, face_ids: list[str]) -> None:
        """Batch assign faces to a person (no commit)."""
        for fid in face_ids:
            match_id = "fpm_" + str(ULID())
            self._session.add(FacePersonMatch(
                match_id=match_id,
                face_id=fid,
                person_id=person_id,
            ))
        self._session.flush()
        # Sync denormalized faces.person_id
        if face_ids:
            self._session.execute(
                text("UPDATE faces SET person_id = :pid WHERE face_id = ANY(:fids)"),
                {"pid": person_id, "fids": face_ids},
            )
            _mark_clusters_dirty(self._session)

    def _recompute_centroid(self, person_id: str) -> None:
        """Recompute centroid_vector as mean of all matched face embeddings (no commit)."""
        self._session.execute(
            text("""
                UPDATE people SET centroid_vector = sub.avg_vec
                FROM (
                    SELECT AVG(f.embedding_vector) AS avg_vec
                    FROM faces f
                    JOIN face_person_matches m ON m.face_id = f.face_id
                    WHERE m.person_id = :pid AND f.embedding_vector IS NOT NULL
                ) sub
                WHERE person_id = :pid
            """),
            {"pid": person_id},
        )
        # Update confirmation_count
        count = self._session.execute(
            text("SELECT COUNT(*)::int FROM face_person_matches WHERE person_id = :pid AND confirmed = TRUE"),
            {"pid": person_id},
        ).scalar() or 0
        self._session.execute(
            text("UPDATE people SET confirmation_count = :cnt WHERE person_id = :pid"),
            {"cnt": count, "pid": person_id},
        )

    def merge(self, target_person_id: str, source_person_id: str) -> Person | None:
        """Merge source person into target. Returns updated target, or None if either not found.

        Atomic: reassign all matches from source to target, update faces.person_id,
        recompute centroid, pick best representative, delete source.
        Uses SELECT ... FOR UPDATE on source to serialize concurrent merges.
        """
        # Lock source to serialize concurrent merges
        source = self._session.execute(
            text("SELECT person_id FROM people WHERE person_id = :pid FOR UPDATE"),
            {"pid": source_person_id},
        ).first()
        if source is None:
            return None

        target = self.get_by_id(target_person_id)
        if target is None:
            return None

        # Reassign face_person_matches from source to target
        self._session.execute(
            text("UPDATE face_person_matches SET person_id = :tid WHERE person_id = :sid"),
            {"tid": target_person_id, "sid": source_person_id},
        )
        # Update denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = :tid WHERE person_id = :sid"),
            {"tid": target_person_id, "sid": source_person_id},
        )

        # Recompute centroid on target
        self._recompute_centroid(target_person_id)

        # Pick best representative face from merged set
        best_face_id = self._session.execute(
            text(
                "SELECT f.face_id FROM faces f "
                "JOIN face_person_matches m ON m.face_id = f.face_id "
                "WHERE m.person_id = :pid "
                "ORDER BY f.detection_confidence DESC NULLS LAST "
                "LIMIT 1"
            ),
            {"pid": target_person_id},
        ).scalar()
        if best_face_id:
            target.representative_face_id = best_face_id

        # Delete source person
        self._session.execute(
            text("DELETE FROM people WHERE person_id = :pid"),
            {"pid": source_person_id},
        )

        _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(target)
        return target
