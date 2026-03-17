"""Repository classes for the tenant database. All take session: Session in constructor."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta

from sqlalchemy import and_, func, insert, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from src.core.io_utils import normalize_path_prefix
from src.core.utils import utcnow
from src.core import asset_status
from src.models.filter import AssetFilterSpec
from src.models.similarity import SimilarityScope
from src.models.tenant import (
    Asset,
    AssetEmbedding,
    AssetMetadata,
    Library,
    LibraryPathFilter,
    Scan,
    SearchSyncQueue,
    TenantPathFilterDefault,
    VideoIndexChunk,
    VideoScene,
    WorkerJob,
)
from ulid import ULID

# Canonical view for non-trashed assets. Use in raw SQL (e.g. FROM active_assets).
ACTIVE_ASSETS = "active_assets"


class PipelineLockHeldError(Exception):
    """Raised when try_acquire finds a lock held by another process with a fresh heartbeat."""

    def __init__(self, hostname: str, pid: int, started_at: datetime) -> None:
        self.hostname = hostname
        self.pid = pid
        self.started_at = started_at
        super().__init__(f"Pipeline lock held by {hostname} pid={pid} since {started_at}")


class LibraryRepository:
    """Repository for libraries table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str, root_path: str, vision_model_id: str = "") -> Library:
        """Generate library_id as lib_ + ULID(), insert, return Library."""
        library_id = "lib_" + str(ULID())
        library = Library(
            library_id=library_id,
            name=name,
            root_path=root_path,
            scan_status="idle",
            vision_model_id=vision_model_id,
        )
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

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
        # Cancel pending/claimed jobs for assets in this library
        self._session.execute(
            text(
                """
                UPDATE worker_jobs SET status = 'cancelled'
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                AND status IN ('pending', 'claimed')
                """
            ),
            {"library_id": library_id},
        )
        library.status = "trashed"
        library.updated_at = utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def hard_delete(self, library_id: str) -> None:
        """Permanently delete library and all related data in FK-safe order. Single transaction."""
        # Order: worker_jobs, search_sync_queue, asset_metadata, video_scenes, video_index_chunks, assets, scans, libraries
        params = {"library_id": library_id}
        self._session.execute(
            text(
                """
                DELETE FROM worker_jobs
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM search_sync_queue
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
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
        self._session.execute(text("DELETE FROM scans WHERE library_id = :library_id"), params)
        self._session.execute(
            text("DELETE FROM library_path_filters WHERE library_id = :library_id"), params
        )
        self._session.execute(text("DELETE FROM libraries WHERE library_id = :library_id"), params)
        self._session.commit()

    def update_scan_status(
        self,
        library_id: str,
        status: str,
        error: str | None = None,
    ) -> Library:
        """Update scan_status; when status is 'complete' or 'error' also set last_scan_error and last_scan_at."""
        library = self.get_by_id(library_id)
        if library is None:
            raise ValueError(f"Library not found: {library_id}")
        library.scan_status = status
        if status in ("complete", "error"):
            library.last_scan_at = utcnow()
        if error is not None:
            library.last_scan_error = error
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library


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


class ScanRepository:
    """Repository for scans table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        library_id: str,
        root_path_override: str | None = None,
        worker_id: str | None = None,
        status: str = "running",
        error_message: str | None = None,
    ) -> Scan:
        """Generate scan_id as scan_ + ULID(), insert, return Scan."""
        scan_id = "scan_" + str(ULID())
        scan = Scan(
            scan_id=scan_id,
            library_id=library_id,
            status=status,
            root_path_override=root_path_override,
            worker_id=worker_id,
            error_message=error_message,
        )
        self._session.add(scan)
        self._session.commit()
        self._session.refresh(scan)
        return scan

    def get_by_id(self, scan_id: str) -> Scan | None:
        """Return scan by id or None."""
        stmt = select(Scan).where(Scan.scan_id == scan_id)
        return self._session.exec(stmt).first()

    def get_running_scans(self, library_id: str) -> list[Scan]:
        """Return scans with status='running' and started_at within last 2 minutes (staleness threshold)."""
        threshold = utcnow() - timedelta(minutes=2)
        stmt = (
            select(Scan)
            .where(Scan.library_id == library_id)
            .where(Scan.status == "running")
            .where(Scan.started_at > threshold)
        )
        return list(self._session.exec(stmt).all())

    def record_batch_counts(
        self,
        scan_id: str,
        added: int,
        updated: int,
        skipped: int,
        missing: int,
    ) -> None:
        """Accumulate batch counts on scan record. Use COALESCE for initial NULLs."""
        self._session.execute(
            text(
                """
                UPDATE scans SET
                    files_added = COALESCE(files_added, 0) + :added,
                    files_updated = COALESCE(files_updated, 0) + :updated,
                    files_skipped = COALESCE(files_skipped, 0) + :skipped,
                    files_missing = COALESCE(files_missing, 0) + :missing,
                    files_discovered = COALESCE(files_discovered, 0) + :added + :updated + :skipped
                WHERE scan_id = :scan_id
                """
            ),
            {
                "scan_id": scan_id,
                "added": added,
                "updated": updated,
                "skipped": skipped,
                "missing": missing,
            },
        )
        self._session.commit()

    def complete(self, scan_id: str, counts: dict) -> Scan:
        """Set status='complete', completed_at=now(), and count fields from counts dict."""
        scan = self.get_by_id(scan_id)
        if scan is None:
            raise ValueError(f"Scan not found: {scan_id}")
        scan.status = "complete"
        scan.completed_at = utcnow()
        scan.files_discovered = counts.get("files_discovered")
        scan.files_added = counts.get("files_added")
        scan.files_updated = counts.get("files_updated")
        scan.files_skipped = counts.get("files_skipped")
        scan.files_missing = counts.get("files_missing")
        self._session.add(scan)
        self._session.commit()
        self._session.refresh(scan)
        return scan

    def abort(self, scan_id: str, error_message: str | None = None) -> Scan:
        """Set status='aborted' or 'error', completed_at=now()."""
        scan = self.get_by_id(scan_id)
        if scan is None:
            raise ValueError(f"Scan not found: {scan_id}")
        scan.status = "error" if error_message else "aborted"
        scan.completed_at = utcnow()
        if error_message is not None:
            scan.error_message = error_message
        self._session.add(scan)
        self._session.commit()
        self._session.refresh(scan)
        return scan


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

    def create_or_update_for_scan_bulk(
        self,
        library_id: str,
        scan_id: str,
        items: list[dict],
    ) -> int:
        """Insert or update assets by (library_id, rel_path). Each item: rel_path, file_size, file_mtime, media_type."""
        if not items:
            return 0
        now = utcnow()
        values = []
        for it in items:
            file_mtime_dt: datetime | None = None
            if it.get("file_mtime"):
                try:
                    fm = it["file_mtime"]
                    if isinstance(fm, str):
                        fm = fm.replace("Z", "+00:00")
                    file_mtime_dt = datetime.fromisoformat(fm) if isinstance(fm, str) else fm
                except (ValueError, TypeError):
                    pass
            asset_id = "ast_" + str(ULID())
            values.append({
                "asset_id": asset_id,
                "library_id": library_id,
                "rel_path": it["rel_path"],
                "file_size": it["file_size"],
                "file_mtime": file_mtime_dt,
                "media_type": it["media_type"],
                "status": asset_status.PENDING,
                "availability": "online",
                "last_scan_id": scan_id,
                "created_at": now,
                "updated_at": now,
            })
        stmt = pg_insert(Asset).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["library_id", "rel_path"],
            set_={
                "file_size": stmt.excluded.file_size,
                "file_mtime": stmt.excluded.file_mtime,
                "status": "pending",
                "availability": "online",
                "last_scan_id": scan_id,
                "updated_at": now,
            },
        )
        self._session.execute(stmt)
        self._session.commit()
        return len(items)

    def create_for_scan(
        self,
        library_id: str,
        rel_path: str,
        file_size: int,
        file_mtime: datetime | None,
        media_type: str,
        scan_id: str,
    ) -> Asset:
        """Create asset with status='pending', availability='online', last_scan_id=scan_id."""
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
            last_scan_id=scan_id,
        )
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def update_for_scan(
        self,
        asset_id: str,
        file_size: int,
        file_mtime: datetime | None,
        availability: str,
        status: str,
        last_scan_id: str,
        media_type: str | None = None,
    ) -> Asset:
        """Update asset file_size, file_mtime, availability, status, last_scan_id."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.file_size = file_size
        asset.file_mtime = file_mtime
        asset.availability = availability
        asset.status = status
        asset.last_scan_id = last_scan_id
        if media_type is not None:
            asset.media_type = media_type
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def touch_for_scan(self, asset_id: str, last_scan_id: str) -> Asset:
        """Update last_scan_id and availability='online' only (for skipped)."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.last_scan_id = last_scan_id
        asset.availability = "online"
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def touch_for_scan_bulk(self, asset_ids: list[str], scan_id: str) -> int:
        """Bulk update last_scan_id and availability='online'. Returns count updated."""
        if not asset_ids:
            return 0
        for batch_start in range(0, len(asset_ids), 500):
            batch = asset_ids[batch_start : batch_start + 500]
            self._session.execute(
                text(
                    """
                    UPDATE assets SET last_scan_id = :scan_id, availability = 'online'
                    WHERE asset_id = ANY(:asset_ids)
                    """
                ),
                {"scan_id": scan_id, "asset_ids": batch},
            )
        self._session.commit()
        return len(asset_ids)

    def set_missing_bulk(self, asset_ids: list[str], scan_id: str) -> int:
        """Set availability='missing' and last_scan_id for given asset_ids. Returns count updated."""
        if not asset_ids:
            return 0
        for batch_start in range(0, len(asset_ids), 500):
            batch = asset_ids[batch_start : batch_start + 500]
            self._session.execute(
                text(
                    """
                    UPDATE assets SET availability = 'missing', last_scan_id = :scan_id
                    WHERE asset_id = ANY(:asset_ids)
                    """
                ),
                {"scan_id": scan_id, "asset_ids": batch},
            )
        self._session.commit()
        return len(asset_ids)

    def mark_missing_for_scan(self, library_id: str, scan_id: str) -> int:
        """Set availability='missing' for active assets in library not seen in this scan (online only). Return count updated."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
            .where(Asset.availability == "online")
            .where((Asset.last_scan_id != scan_id) | (Asset.last_scan_id.is_(None)))
        )
        assets = list(self._session.exec(stmt).all())
        for asset in assets:
            asset.availability = "missing"
            self._session.add(asset)
        self._session.commit()
        return len(assets)

    def get_by_id(self, asset_id: str) -> Asset | None:
        """Return asset by id or None."""
        return self._session.get(Asset, asset_id)

    def list_pending_by_library(self, library_id: str) -> list[Asset]:
        """Return all assets in library with status='pending'."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .where(Asset.status == "pending")
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

    def count_by_library(self, library_id: str) -> int:
        """Return total active asset count for library."""
        result = self._session.execute(
            text(
                "SELECT COUNT(*)::int FROM active_assets WHERE library_id = :library_id"
            ),
            {"library_id": library_id},
        )
        return int(result.scalar() or 0)

    def page_by_library(
        self,
        library_id: str,
        after: str | None,
        limit: int,
        path_prefix: str | None = None,
        tag: str | None = None,
    ) -> list[Asset]:
        """Keyset pagination: return assets with asset_id > after, ordered by asset_id, limit rows.

        Optional path_prefix filters to assets whose rel_path equals the prefix
        or starts with prefix + '/'. The prefix is expected to be normalized
        (no leading/trailing slash).

        Optional tag filters to assets whose latest metadata row's tags array
        (data->'tags') contains the given tag.
        """
        conditions = ["a.library_id = :library_id"]
        params: dict[str, object] = {
            "library_id": library_id,
            "limit": limit,
        }
        if path_prefix:
            conditions.append(
                "(a.rel_path = :path_prefix OR a.rel_path LIKE :path_prefix_like)"
            )
            params["path_prefix"] = path_prefix
            params["path_prefix_like"] = path_prefix + "/%"
        if after is not None:
            conditions.append("a.asset_id > :after")
            params["after"] = after
        if tag is not None:
            conditions.append("m.tags @> jsonb_build_array(:tag)")
            params["tag"] = tag

        where_sql = " AND ".join(conditions)
        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            LEFT JOIN LATERAL (
                SELECT data->'tags' AS tags
                FROM asset_metadata
                WHERE asset_id = a.asset_id
                ORDER BY generated_at DESC
                LIMIT 1
            ) m ON TRUE
            WHERE {where_sql}
            ORDER BY a.asset_id
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
            text("DELETE FROM worker_jobs WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM search_sync_queue WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
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
    ) -> Asset:
        """Update asset proxy_key, thumbnail_key, width, height, status='proxy_ready', updated_at."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = proxy_key
        asset.thumbnail_key = thumbnail_key
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
        """Record a thumbnail_key on the asset. Used by VideoIndexWorker after extracting first frame."""
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
                    duration_sec = COALESCE(:duration_sec, duration_sec)
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

    def query_for_enqueue(
        self,
        filter: AssetFilterSpec,
        job_type: str,
        force: bool,
    ) -> list[str]:
        """
        Return asset_ids matching filter spec, suitable for job enqueueing.

        If retry_failed=True: only assets that have a failed job of this type,
        and no pending/claimed/completed job. Excludes already-processed checks.

        If force=False and not retry_failed: excludes assets that already have
        a pending/claimed job, and excludes assets where proxy_key/thumbnail_key
        is already set (for proxy/thumbnail job types).

        If force=True: returns all matching assets regardless of existing jobs.

        Resolution order: if asset_id is set, all other filters ignored.
        """
        conditions = ["a.library_id = :library_id"]
        params: dict = {"library_id": filter.library_id, "job_type": job_type}
        join_libraries = filter.missing_ai or (job_type == "ai_vision" and not force and not filter.retry_failed)

        # Single asset shortcut
        if filter.asset_id:
            conditions.append("a.asset_id = :asset_id")
            params["asset_id"] = filter.asset_id
        else:
            if filter.path_exact:
                conditions.append("a.rel_path = :path_exact")
                params["path_exact"] = filter.path_exact
            elif filter.path_prefix:
                conditions.append("a.rel_path LIKE :path_prefix")
                params["path_prefix"] = filter.path_prefix.rstrip("/") + "/%"
            if filter.mtime_after:
                conditions.append("a.file_mtime >= :mtime_after")
                params["mtime_after"] = filter.mtime_after
            if filter.mtime_before:
                conditions.append("a.file_mtime <= :mtime_before")
                params["mtime_before"] = filter.mtime_before
            if filter.missing_proxy:
                conditions.append("a.proxy_key IS NULL")
            if filter.missing_thumbnail:
                conditions.append("a.thumbnail_key IS NULL")
            if filter.missing_ai:
                conditions.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM asset_metadata m
                        WHERE m.asset_id = a.asset_id
                          AND m.model_id = l.vision_model_id
                    )
                    """
                )
            if filter.camera_make:
                conditions.append("a.camera_make ILIKE :camera_make")
                params["camera_make"] = f"%{filter.camera_make}%"
            if filter.camera_model:
                conditions.append("a.camera_model ILIKE :camera_model")
                params["camera_model"] = f"%{filter.camera_model}%"
            if filter.missing_exif:
                conditions.append("a.exif_extracted_at IS NULL")
            if filter.taken_after:
                conditions.append("a.taken_at >= :taken_after")
                params["taken_after"] = filter.taken_after
            if filter.taken_before:
                conditions.append("a.taken_at <= :taken_before")
                params["taken_before"] = filter.taken_before

        if filter.retry_failed:
            # Only assets with a failed job of this type; exclude those actively
            # being worked (pending/claimed). Old completed records are intentionally
            # allowed — --force re-runs leave historical completed rows behind, and
            # retry_failed should still be able to reset a newer failed job.
            conditions.append(
                """
                EXISTS (
                    SELECT 1 FROM worker_jobs w
                    WHERE w.asset_id = a.asset_id
                      AND w.job_type = :job_type
                      AND w.status = 'failed'
                )
                """
            )
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM worker_jobs w
                    WHERE w.asset_id = a.asset_id
                      AND w.job_type = :job_type
                      AND w.status IN ('pending', 'claimed')
                )
                """
            )
            # Keep enqueue eligibility invariants even for retries.
            # Otherwise --retry-failed can resurrect permanently-invalid work.
            if job_type == "ai_vision":
                # ai_vision runs only on image proxies (video uses video-vision).
                conditions.append("a.proxy_key IS NOT NULL")
                conditions.append("a.media_type LIKE 'image%'")
            elif job_type == "embed":
                conditions.append("a.proxy_key IS NOT NULL")
            elif job_type in ("video-index", "video-preview", "video-vision"):
                conditions.append("a.media_type = 'video'")
        elif not force:
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM worker_jobs w
                    WHERE w.asset_id = a.asset_id
                      AND w.job_type = :job_type
                      AND w.status IN ('pending', 'claimed')
                )
                """
            )
            # Exclude already-processed assets for proxy/thumbnail/exif job types
            if job_type == "proxy":
                conditions.append("a.proxy_key IS NULL")
            elif job_type == "thumbnail":
                conditions.append("a.thumbnail_key IS NULL")
            elif job_type == "exif":
                conditions.append("a.exif_extracted_at IS NULL")
            elif job_type == "ai_vision":
                # ai_vision runs only on image proxies (video uses video-vision).
                # Ensure we never enqueue ai_vision for assets without a proxy_key
                # (e.g. when video proxy generation is deferred).
                conditions.append("a.proxy_key IS NOT NULL")
                conditions.append("a.media_type LIKE 'image%'")
                conditions.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM asset_metadata m
                        WHERE m.asset_id = a.asset_id
                          AND m.model_id = l.vision_model_id
                    )
                    """
                )
            elif job_type == "embed":
                conditions.append("a.proxy_key IS NOT NULL")
                conditions.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM asset_embeddings ae
                        WHERE ae.asset_id = a.asset_id
                    )
                    """
                )
            elif job_type in ("video-index", "video-preview", "video-vision"):
                conditions.append("a.media_type = 'video'")
                if job_type in ("video-index", "video-vision"):
                    conditions.append("a.video_indexed IS NOT TRUE")

        where = " AND ".join(conditions)
        from_clause = (
            "FROM active_assets a JOIN libraries l ON l.library_id = a.library_id"
            if join_libraries
            else "FROM active_assets a"
        )
        sql = f"SELECT a.asset_id {from_clause} WHERE {where} ORDER BY a.asset_id"
        rows = self._session.execute(text(sql), params).fetchall()
        return [row[0] for row in rows]


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
                JOIN assets a ON a.asset_id = ae.asset_id
                WHERE {where}
                ORDER BY distance ASC
                LIMIT :limit OFFSET :offset
            """
        rows = self._session.execute(text(sql), params).fetchall()
        return [(r.asset_id, float(r.distance)) for r in rows]


FAILURE_BLOCK_THRESHOLD = 3


class WorkerJobRepository:
    """Repository for worker_jobs table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, job_id: str) -> WorkerJob | None:
        """Return job by id or None."""
        return self._session.get(WorkerJob, job_id)

    def create(self, job_type: str, asset_id: str, priority: int = 10) -> WorkerJob:
        """Create a pending job. job_id = job_ + ULID."""
        job_id = "job_" + str(ULID())
        job = WorkerJob(
            job_id=job_id,
            job_type=job_type,
            asset_id=asset_id,
            status="pending",
            priority=priority,
        )
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)
        return job

    def has_pending_job(self, job_type: str, asset_id: str) -> bool:
        """Return True if there is a pending or claimed job of this type for this asset."""
        stmt = (
            select(WorkerJob)
            .where(WorkerJob.job_type == job_type)
            .where(WorkerJob.asset_id == asset_id)
            .where(WorkerJob.status.in_(["pending", "claimed"]))
        )
        return self._session.exec(stmt).first() is not None

    def pending_count(
        self,
        job_type: str,
        library_id: str | None = None,
        path_prefix: str | None = None,
    ) -> int:
        """Count jobs with status pending or claimed.

        Same filters as claim_next (job_type, optional library_id and path_prefix).
        """
        stmt = (
            select(func.count())
            .select_from(WorkerJob)
            .where(
                WorkerJob.job_type == job_type,
                WorkerJob.status.in_(["pending", "claimed"]),
            )
        )
        if library_id is not None or path_prefix is not None:
            stmt = stmt.join(Asset, WorkerJob.asset_id == Asset.asset_id)
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                stmt = stmt.where(Asset.rel_path.like(normalised.rstrip("/") + "/%"))
        result = self._session.execute(stmt)
        return int(result.scalar() or 0)

    def claim_next(
        self,
        job_type: str,
        worker_id: str,
        lease_minutes: int,
        library_id: str | None = None,
        path_prefix: str | None = None,
    ) -> WorkerJob | None:
        """Claim next pending (or expired claimed) job with FOR UPDATE SKIP LOCKED. Return None if none.

        Jobs are claimed in priority order (lower priority value first), then by created_at.
        """
        now = utcnow()
        stmt = select(WorkerJob).where(WorkerJob.job_type == job_type).where(
            or_(
                WorkerJob.status == "pending",
                and_(
                    WorkerJob.status == "claimed",
                    WorkerJob.lease_expires_at < now,
                ),
            )
        )
        if library_id is not None or path_prefix is not None:
            stmt = stmt.join(Asset, WorkerJob.asset_id == Asset.asset_id)
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                stmt = stmt.where(Asset.rel_path.like(normalised.rstrip("/") + "/%"))
        stmt = (
            stmt.order_by(WorkerJob.priority, WorkerJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = self._session.exec(stmt).first()
        if job is None:
            return None
        job.status = "claimed"
        job.worker_id = worker_id
        job.claimed_at = now
        job.lease_expires_at = now + timedelta(minutes=lease_minutes)
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)
        return job

    def set_completed(self, job: WorkerJob) -> None:
        """Set job status to completed and completed_at."""
        job.status = "completed"
        job.completed_at = utcnow()
        job.error_message = None
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)

    def set_failed(self, job: WorkerJob, error_message: str) -> None:
        """Increment fail_count and set status to failed, or blocked once threshold is reached."""
        job.fail_count = (job.fail_count or 0) + 1
        job.status = "blocked" if job.fail_count >= FAILURE_BLOCK_THRESHOLD else "failed"
        job.completed_at = utcnow()
        job.error_message = error_message
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)

    def cancel_pending_for_assets(self, asset_ids: list[str], job_type: str) -> int:
        """Cancel pending/claimed/blocked jobs for given assets and job_type. Used by force enqueue."""
        if not asset_ids:
            return 0
        result = self._session.execute(
            text("""
                UPDATE worker_jobs
                SET status = 'cancelled'
                WHERE asset_id = ANY(:asset_ids)
                  AND job_type = :job_type
                  AND status IN ('pending', 'claimed', 'blocked')
            """),
            {"asset_ids": asset_ids, "job_type": job_type},
        )
        self._session.commit()
        return result.rowcount

    def cancel_failed_for_assets(self, asset_ids: list[str], job_type: str) -> int:
        """Cancel failed jobs for given assets and job_type."""
        if not asset_ids:
            return 0
        result = self._session.execute(
            text("""
                UPDATE worker_jobs
                SET status = 'cancelled'
                WHERE asset_id = ANY(:asset_ids)
                  AND job_type = :job_type
                  AND status = 'failed'
            """),
            {"asset_ids": asset_ids, "job_type": job_type},
        )
        self._session.commit()
        return result.rowcount

    def reset_failed_to_pending(self, asset_ids: list[str], job_type: str) -> int:
        """
        Reset failed (non-blocked) jobs back to pending, preserving fail_count so it
        continues to count toward the block threshold. Used by the retry_failed enqueue path.
        """
        if not asset_ids:
            return 0
        result = self._session.execute(
            text("""
                UPDATE worker_jobs
                SET status = 'pending',
                    worker_id = NULL,
                    claimed_at = NULL,
                    lease_expires_at = NULL,
                    completed_at = NULL,
                    error_message = NULL
                WHERE asset_id = ANY(:asset_ids)
                  AND job_type = :job_type
                  AND status = 'failed'
            """),
            {"asset_ids": asset_ids, "job_type": job_type},
        )
        self._session.commit()
        return result.rowcount

    def pipeline_status(self, library_id: str) -> list[dict]:
        """
        Return [{job_type, status, count}] for all jobs in library.
        Uses latest-state per (asset_id, job_type) — counts reflect
        current state, not historical retries.
        """
        rows = self._session.execute(
            text("""
                WITH latest_jobs AS (
                    SELECT DISTINCT ON (wj.asset_id, wj.job_type)
                        wj.asset_id,
                        wj.job_type,
                        wj.status
                    FROM worker_jobs wj
                    JOIN assets a ON a.asset_id = wj.asset_id
                    WHERE a.library_id = :library_id
                    ORDER BY wj.asset_id, wj.job_type, wj.created_at DESC
                )
                SELECT job_type, status, COUNT(*)::int as count
                FROM latest_jobs
                GROUP BY job_type, status
                ORDER BY job_type, status
            """),
            {"library_id": library_id},
        ).fetchall()
        return [{"job_type": r.job_type, "status": r.status, "count": r.count} for r in rows]

    def pipeline_status_tenant(self) -> list[dict]:
        """
        Return [{library_id, job_type, status, count}] across all libraries.
        Same logic as pipeline_status but without library filter.
        """
        rows = self._session.execute(
            text("""
                WITH latest_jobs AS (
                    SELECT DISTINCT ON (wj.asset_id, wj.job_type)
                        a.library_id,
                        wj.job_type,
                        wj.status
                    FROM worker_jobs wj
                    JOIN assets a ON a.asset_id = wj.asset_id
                    ORDER BY wj.asset_id, wj.job_type, wj.created_at DESC
                )
                SELECT library_id, job_type, status, COUNT(*)::int as count
                FROM latest_jobs
                GROUP BY library_id, job_type, status
                ORDER BY library_id, job_type, status
            """),
        ).fetchall()
        return [{"library_id": r.library_id, "job_type": r.job_type, "status": r.status, "count": r.count} for r in rows]

    def active_worker_count(self, library_id: str | None = None) -> int:
        """
        Count distinct workers active in the last 60 seconds.
        A worker is active if it:
          - Currently holds a claimed job with a valid lease, OR
          - Claimed or completed/failed a job within the last 60 seconds.
        Optionally scoped to a specific library.
        """
        if library_id:
            rows = self._session.execute(
                text("""
                    SELECT COUNT(DISTINCT wj.worker_id)::int
                    FROM worker_jobs wj
                    JOIN assets a ON a.asset_id = wj.asset_id
                    WHERE a.library_id = :library_id
                      AND wj.worker_id IS NOT NULL
                      AND (
                          (wj.status = 'claimed' AND wj.lease_expires_at > NOW())
                          OR wj.claimed_at > NOW() - INTERVAL '60 seconds'
                          OR wj.completed_at > NOW() - INTERVAL '60 seconds'
                      )
                """),
                {"library_id": library_id},
            ).fetchone()
        else:
            rows = self._session.execute(
                text("""
                    SELECT COUNT(DISTINCT worker_id)::int
                    FROM worker_jobs
                    WHERE worker_id IS NOT NULL
                      AND (
                          (status = 'claimed' AND lease_expires_at > NOW())
                          OR claimed_at > NOW() - INTERVAL '60 seconds'
                          OR completed_at > NOW() - INTERVAL '60 seconds'
                      )
                """),
            ).fetchone()
        return rows[0] if rows else 0

    def list_failures(
        self,
        library_id: str,
        job_type: str,
        path_prefix: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict], int]:
        """
        Return (rows, total_count) where rows are the most recent failed job per asset.
        total_count is the unfiltered count of distinct assets with failures.
        Each row: {rel_path, error_message, failed_at}

        DISTINCT ON (asset_id) is correct here: we filter to a single
        job_type and status='failed', so we get the most recent failed
        job per asset within that type. No job_type in the DISTINCT needed.
        """
        path_filter = ""
        params: dict = {
            "library_id": library_id,
            "job_type": job_type,
        }
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                path_filter = " AND (a.rel_path = :path_exact OR a.rel_path LIKE :path_pattern)"
                params["path_exact"] = normalised
                params["path_pattern"] = normalised + "/%"

        # Total count (distinct assets where the latest job is failed, with optional path filter).
        # DISTINCT ON (asset_id) is correct: job_type is in WHERE, so we get one row per asset
        # for this job type; we then filter to only those whose latest status is failed.
        count_sql = f"""
            SELECT COUNT(*)::int FROM (
                SELECT DISTINCT ON (wj.asset_id)
                    wj.asset_id,
                    wj.status
                FROM worker_jobs wj
                JOIN assets a ON a.asset_id = wj.asset_id
                WHERE a.library_id = :library_id
                  AND wj.job_type = :job_type
                  {path_filter}
                ORDER BY wj.asset_id, wj.created_at DESC
            ) latest
            WHERE latest.status = 'failed'
        """
        total = int(
            self._session.execute(text(count_sql), params).scalar() or 0
        )

        # Rows: most recent job per asset; filtered after DISTINCT to only those still failed.
        params["limit"] = limit
        rows_sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (wj.asset_id)
                    a.rel_path,
                    wj.status,
                    wj.error_message,
                    wj.completed_at
                FROM worker_jobs wj
                JOIN assets a ON a.asset_id = wj.asset_id
                WHERE a.library_id = :library_id
                  AND wj.job_type = :job_type
                  {path_filter}
                ORDER BY wj.asset_id, wj.created_at DESC
            ) latest
            WHERE latest.status = 'failed'
            ORDER BY rel_path
            LIMIT :limit
        """
        rows = self._session.execute(text(rows_sql), params).fetchall()
        return (
            [
                {
                    "rel_path": r.rel_path,
                    "error_message": r.error_message or "",
                    "failed_at": r.completed_at,
                }
            for r in rows
        ], total)


class PipelineLockRepository:
    """Repository for pipeline_locks table (raw SQL, no model). One logical lock per tenant."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def try_acquire(self, tenant_id: str, lock_timeout_minutes: int = 5) -> bool:
        """
        Acquire the pipeline lock for the tenant if no row exists or the existing lock is stale.
        Return True on success. Raise PipelineLockHeldError if another process holds a fresh lock.
        """
        now = utcnow()
        stale_threshold = now - timedelta(minutes=lock_timeout_minutes)
        row = self._session.execute(
            text(
                """
                SELECT lock_id, hostname, pid, started_at, heartbeat_at
                FROM pipeline_locks
                WHERE tenant_id = :tenant_id
                """
            ),
            {"tenant_id": tenant_id},
        ).fetchone()
        if row is None:
            lock_id = "lock_" + str(ULID())
            self._session.execute(
                text(
                    """
                    INSERT INTO pipeline_locks (lock_id, tenant_id, hostname, pid, started_at, heartbeat_at)
                    VALUES (:lock_id, :tenant_id, :hostname, :pid, :started_at, :heartbeat_at)
                    """
                ),
                {
                    "lock_id": lock_id,
                    "tenant_id": tenant_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "started_at": now,
                    "heartbeat_at": now,
                },
            )
            self._session.commit()
            return True
        _lock_id, hostname, pid, started_at, heartbeat_at = row
        if heartbeat_at is not None and heartbeat_at > stale_threshold:
            raise PipelineLockHeldError(hostname, pid, started_at)
        lock_id = "lock_" + str(ULID())
        self._session.execute(
            text(
                """
                UPDATE pipeline_locks
                SET lock_id = :lock_id, hostname = :hostname, pid = :pid,
                    started_at = :started_at, heartbeat_at = :heartbeat_at
                WHERE tenant_id = :tenant_id
                """
            ),
            {
                "lock_id": lock_id,
                "tenant_id": tenant_id,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "started_at": now,
                "heartbeat_at": now,
            },
        )
        self._session.commit()
        return True

    def force_acquire(self, tenant_id: str) -> None:
        """Delete any existing lock for the tenant and insert a new one."""
        now = utcnow()
        self._session.execute(
            text("DELETE FROM pipeline_locks WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        lock_id = "lock_" + str(ULID())
        self._session.execute(
            text(
                """
                INSERT INTO pipeline_locks (lock_id, tenant_id, hostname, pid, started_at, heartbeat_at)
                VALUES (:lock_id, :tenant_id, :hostname, :pid, :started_at, :heartbeat_at)
                """
            ),
            {
                "lock_id": lock_id,
                "tenant_id": tenant_id,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "started_at": now,
                "heartbeat_at": now,
            },
        )
        self._session.commit()

    def heartbeat(self, tenant_id: str) -> None:
        """Update heartbeat_at to now for the tenant's lock."""
        self._session.execute(
            text(
                "UPDATE pipeline_locks SET heartbeat_at = NOW() WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        )
        self._session.commit()

    def release(self, tenant_id: str) -> None:
        """Remove the lock row for the tenant."""
        self._session.execute(
            text("DELETE FROM pipeline_locks WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        self._session.commit()


class SearchSyncQueueRepository:
    """Repository for search_sync_queue outbox table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        asset_id: str,
        operation: str,
        scene_id: str | None = None,
    ) -> SearchSyncQueue | None:
        """
        Insert a new row into search_sync_queue.

        sync_id is generated as ssq_ + ULID(). Returns None and skips insert if
        a synced row already exists for this (asset_id, scene_id) with
        created_at >= the new row's created_at (avoids creating a stale pending
        row that would be hidden by search_sync_latest).
        """
        now = utcnow()
        exists = self._session.execute(
            text(
                """
                SELECT 1 FROM search_sync_queue
                WHERE asset_id = :asset_id
                  AND (scene_id IS NOT DISTINCT FROM :scene_id)
                  AND status = 'synced'
                  AND created_at >= :created_at
                LIMIT 1
                """
            ),
            {"asset_id": asset_id, "scene_id": scene_id, "created_at": now},
        ).scalar()
        if exists:
            return None
        sync = SearchSyncQueue(
            sync_id="ssq_" + str(ULID()),
            asset_id=asset_id,
            scene_id=scene_id,
            operation=operation,
            status="pending",
            created_at=now,
        )
        self._session.add(sync)
        self._session.commit()
        self._session.refresh(sync)
        return sync

    def claim_batch(
        self,
        batch_size: int = 100,
        library_id: str | None = None,
        path_prefix: str | None = None,
        lease_minutes: int = 5,
    ) -> list[SearchSyncQueue]:
        """
        Claim up to batch_size assets whose latest sync state is pending.

        Uses search_sync_latest view to pick only assets whose most recent queue row
        is pending (deduplicates retries/force-resyncs). Locks underlying table rows.
        Returns claimed rows with status updated to 'processing'.
        """
        library_filter = " AND a.library_id = :library_id" if library_id else ""
        path_filter = ""
        params: dict = {"batch_size": batch_size}
        if library_id:
            params["library_id"] = library_id
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                path_filter = " AND a.rel_path LIKE :path_prefix"
                params["path_prefix"] = normalised + "/%"
        params["lease_interval"] = f"{lease_minutes} minutes"

        sql = f"""
            WITH candidates AS (
                SELECT ssl.sync_id, ssl.asset_id
                FROM search_sync_latest ssl
                JOIN assets a ON a.asset_id = ssl.asset_id
                WHERE ssl.status = 'pending'
                   OR (ssl.status = 'processing'
                       AND ssl.processing_started_at < NOW() - (:lease_interval)::interval)
                {library_filter}
                {path_filter}
                ORDER BY ssl.created_at
                LIMIT :batch_size
            ),
            locked AS (
                SELECT ssq.sync_id, ssq.asset_id
                FROM search_sync_queue ssq
                JOIN candidates c ON c.sync_id = ssq.sync_id
                FOR UPDATE OF ssq SKIP LOCKED
            )
            UPDATE search_sync_queue
            SET status = 'processing',
                processing_started_at = NOW()
            WHERE sync_id IN (SELECT sync_id FROM locked)
            RETURNING sync_id, asset_id, operation
        """
        result = self._session.execute(text(sql), params)
        rows_data = result.fetchall()
        self._session.commit()
        if not rows_data:
            return []
        sync_ids = [r[0] for r in rows_data]
        rows = []
        for sid in sync_ids:
            row = self._session.get(SearchSyncQueue, sid)
            if row is not None:
                rows.append(row)
        return rows

    RESYNC_BATCH_SIZE = 500

    def enqueue_all_for_library(
        self,
        library_id: str,
        path_prefix: str | None = None,
        progress_callback: object | None = None,
    ) -> list[str]:
        """
        Re-enqueue all online, non-trashed assets in the library for search sync.

        Optionally scope by path_prefix (rel_path LIKE prefix/%).
        Processes in batches of RESYNC_BATCH_SIZE. If progress_callback is provided,
        it is called after each batch as progress_callback(completed, total).
        Returns the list of asset_ids enqueued.
        """
        sql = """
            SELECT asset_id FROM active_assets
            WHERE library_id = :library_id
            AND availability = 'online'
            AND status != 'trashed'
        """
        params: dict = {"library_id": library_id}
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                sql += " AND rel_path LIKE :path_prefix"
                params["path_prefix"] = normalised + "/%"
        asset_ids = [r[0] for r in self._session.execute(text(sql), params).fetchall()]
        if not asset_ids:
            return []

        total = len(asset_ids)
        cb = progress_callback if callable(progress_callback) else None

        for i in range(0, total, self.RESYNC_BATCH_SIZE):
            batch = asset_ids[i : i + self.RESYNC_BATCH_SIZE]

            # Reset existing rows to pending
            self._session.execute(
                text(
                    """
                    UPDATE search_sync_queue
                    SET status = 'pending'
                    WHERE asset_id = ANY(:asset_ids)
                    """
                ),
                {"asset_ids": batch},
            )
            self._session.commit()

            # Find asset_ids in batch that have no row
            rows = self._session.execute(
                text(
                    """
                    SELECT asset_id FROM search_sync_queue
                    WHERE asset_id = ANY(:asset_ids)
                    """
                ),
                {"asset_ids": batch},
            ).fetchall()
            existing = {r[0] for r in rows}
            to_insert = [aid for aid in batch if aid not in existing]

            if to_insert:
                insert_time = utcnow()
                # Exclude asset_ids that already have a synced row with created_at >= insert_time,
                # so we never insert a stale pending row hidden by search_sync_latest.
                already_synced = self._session.execute(
                    text(
                        """
                        SELECT asset_id FROM search_sync_queue
                        WHERE asset_id = ANY(:asset_ids)
                          AND scene_id IS NOT DISTINCT FROM NULL
                          AND status = 'synced'
                          AND created_at >= :insert_time
                        """
                    ),
                    {"asset_ids": to_insert, "insert_time": insert_time},
                ).fetchall()
                skip_ids = {r[0] for r in already_synced}
                to_insert = [aid for aid in to_insert if aid not in skip_ids]

                for aid in to_insert:
                    sync = SearchSyncQueue(
                        sync_id="ssq_" + str(ULID()),
                        asset_id=aid,
                        scene_id=None,
                        operation="index",
                        status="pending",
                        created_at=insert_time,
                    )
                    self._session.add(sync)
                if to_insert:
                    self._session.commit()

            completed = min(i + self.RESYNC_BATCH_SIZE, total)
            if cb:
                cb(completed, total)

        return asset_ids

    def search_sync_pipeline_status(self, library_id: str) -> list[dict]:
        """
        Return [{status, count}] for search_sync_latest in this library.
        Status is synced, pending, or processing. Used for pipeline overview.
        """
        rows = self._session.execute(
            text("""
                SELECT ssl.status, COUNT(*)::int as count
                FROM search_sync_latest ssl
                JOIN assets a ON a.asset_id = ssl.asset_id
                WHERE a.library_id = :library_id
                GROUP BY ssl.status
            """),
            {"library_id": library_id},
        ).fetchall()
        return [{"status": r.status, "count": r.count} for r in rows]

    def search_sync_pipeline_status_tenant(self) -> list[dict]:
        """Return [{library_id, status, count}] across all libraries."""
        rows = self._session.execute(
            text("""
                SELECT a.library_id, ssl.status, COUNT(*)::int as count
                FROM search_sync_latest ssl
                JOIN assets a ON a.asset_id = ssl.asset_id
                GROUP BY a.library_id, ssl.status
            """),
        ).fetchall()
        return [{"library_id": r.library_id, "status": r.status, "count": r.count} for r in rows]

    def pending_count(self, library_id: str | None = None, path_prefix: str | None = None) -> int:
        """
        Count distinct assets whose latest sync state is 'pending'.
        Uses search_sync_latest view for accurate per-asset counts.
        """
        sql = """
            SELECT COUNT(*)
            FROM search_sync_latest ssl
            JOIN assets a ON a.asset_id = ssl.asset_id
            WHERE ssl.status = 'pending'
        """
        params: dict = {}
        if library_id:
            sql += " AND a.library_id = :library_id"
            params["library_id"] = library_id
        if path_prefix:
            normalised = normalize_path_prefix(path_prefix)
            if normalised:
                sql += " AND a.rel_path LIKE :path_prefix"
                params["path_prefix"] = normalised + "/%"
        return int(self._session.execute(text(sql), params).scalar() or 0)

    def mark_synced(self, sync_ids: list[str]) -> int:
        """
        Mark the given sync_ids as synced.

        Returns the number of rows updated.
        """
        if not sync_ids:
            return 0
        result = self._session.execute(
            text(
                """
                UPDATE search_sync_queue
                SET status = 'synced'
                WHERE sync_id = ANY(:sync_ids)
                """
            ),
            {"sync_ids": sync_ids},
        )
        self._session.commit()
        # SQLAlchemy's rowcount can be -1 on some drivers; coerce to int >= 0
        try:
            return int(result.rowcount or 0)
        except Exception:
            return 0


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
        # Must delete search_sync_queue rows that reference these scenes first.
        scene_ids_subq = (
            select(VideoScene.scene_id).where(
                VideoScene.asset_id.in_(  # type: ignore[attr-defined]
                    select(Asset.asset_id).where(
                        Asset.library_id == library_id,
                        Asset.media_type == "video",
                    )
                )
            )
        )
        self._session.exec(  # type: ignore[call-overload]
            sa_delete(SearchSyncQueue).where(
                SearchSyncQueue.scene_id.in_(scene_ids_subq)  # type: ignore[attr-defined]
            )
        )
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
        Also reclaims chunks whose lease has expired.
        Returns None if no chunks available.
        """
        now = utcnow()
        lease_expires = now + timedelta(minutes=self.LEASE_MINUTES)

        # Reclaim expired leases
        expired = self._session.exec(
            select(VideoIndexChunk).where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "claimed",
                VideoIndexChunk.lease_expires_at < now,
            )
        ).all()
        for chunk in expired:
            chunk.status = "pending"
            chunk.worker_id = None
            chunk.claimed_at = None
            chunk.lease_expires_at = None
            self._session.add(chunk)

        chunk = self._session.exec(
            select(VideoIndexChunk)
            .where(
                VideoIndexChunk.asset_id == asset_id,
                VideoIndexChunk.status == "pending",
            )
            .order_by(VideoIndexChunk.chunk_index)
            .limit(1)
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
