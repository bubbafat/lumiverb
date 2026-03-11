"""Repository classes for the tenant database. All take session: Session in constructor."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from src.models.filter import AssetFilterSpec
from src.models.tenant import Asset, AssetEmbedding, AssetMetadata, Library, Scan, SearchSyncQueue, WorkerJob
from ulid import ULID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LibraryRepository:
    """Repository for libraries table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str, root_path: str, vision_model_id: str = "moondream") -> Library:
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
        library.updated_at = _utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def hard_delete(self, library_id: str) -> None:
        """Permanently delete library and all related data in FK-safe order. Single transaction."""
        # Order: worker_jobs, search_sync_queue, asset_metadata, video_scenes, assets, scans, libraries
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
        self._session.execute(text("DELETE FROM assets WHERE library_id = :library_id"), params)
        self._session.execute(text("DELETE FROM scans WHERE library_id = :library_id"), params)
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
            library.last_scan_at = _utcnow()
        if error is not None:
            library.last_scan_error = error
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library


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
        threshold = _utcnow() - timedelta(minutes=2)
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
        scan.completed_at = _utcnow()
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
        scan.completed_at = _utcnow()
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
        now = _utcnow()
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
                "status": "pending",
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
            status="pending",
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
        """Set availability='missing' for assets in library not seen in this scan (online only). Return count updated."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
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
        """Return all assets in library."""
        stmt = select(Asset).where(Asset.library_id == library_id)
        return list(self._session.exec(stmt).all())

    def count_by_library(self, library_id: str) -> int:
        """Return total asset count for library."""
        result = self._session.execute(
            text("SELECT COUNT(*)::int FROM assets WHERE library_id = :library_id"),
            {"library_id": library_id},
        )
        return int(result.scalar() or 0)

    def page_by_library(
        self,
        library_id: str,
        after: str | None,
        limit: int,
    ) -> list[Asset]:
        """Keyset pagination: return assets with asset_id > after, ordered by asset_id, limit rows."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .order_by(Asset.asset_id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(Asset.asset_id > after)
        return list(self._session.exec(stmt).all())

    def list_all(self) -> list[Asset]:
        """Return all assets (all libraries)."""
        return list(self._session.exec(select(Asset)).all())

    def update_proxy(
        self,
        asset_id: str,
        proxy_key: str,
        thumbnail_key: str,
        width: int,
        height: int,
    ) -> Asset:
        """Update asset proxy_key, thumbnail_key, width, height, status='proxied', updated_at."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = proxy_key
        asset.thumbnail_key = thumbnail_key
        asset.width = width
        asset.height = height
        asset.status = "proxied"
        asset.updated_at = _utcnow()
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

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
    ) -> None:
        """Update EXIF fields on asset record."""
        taken_at_dt: datetime | None = None
        if taken_at:
            try:
                taken_at_dt = datetime.fromisoformat(taken_at)
            except ValueError:
                pass
        self._session.execute(
            text("""
                UPDATE assets SET
                    sha256 = :sha256,
                    exif = :exif,
                    exif_extracted_at = :now,
                    camera_make = :camera_make,
                    camera_model = :camera_model,
                    taken_at = :taken_at,
                    gps_lat = :gps_lat,
                    gps_lon = :gps_lon
                WHERE asset_id = :asset_id
            """),
            {
                "sha256": sha256,
                "exif": json.dumps(exif) if exif else None,
                "now": _utcnow(),
                "camera_make": camera_make,
                "camera_model": camera_model,
                "taken_at": taken_at_dt,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "asset_id": asset_id,
            },
        )
        self._session.commit()

    def get_by_ids(self, asset_ids: list[str]) -> list[Asset]:
        """Return assets for a list of asset_ids. Order not guaranteed."""
        if not asset_ids:
            return []
        stmt = select(Asset).where(Asset.asset_id.in_(asset_ids))
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
            # Only assets with a failed job of this type; exclude pending/claimed/completed
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
                      AND w.status IN ('pending', 'claimed', 'completed')
                )
                """
            )
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
                conditions.append(
                    """
                    NOT EXISTS (
                        SELECT 1 FROM asset_metadata m
                        WHERE m.asset_id = a.asset_id
                          AND m.model_id = l.vision_model_id
                    )
                    """
                )

        where = " AND ".join(conditions)
        from_clause = "FROM assets a JOIN libraries l ON l.library_id = a.library_id" if join_libraries else "FROM assets a"
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
        now = _utcnow()
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
            created_at=_utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_embeddings_asset_model_version",
            set_={"embedding_vector": vector, "created_at": _utcnow()},
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
        exclude_asset_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[tuple[str, float]]:
        """
        Return (asset_id, distance) pairs ordered by cosine distance ASC.
        Filters to assets in library_id that are online.
        """
        rows = self._session.execute(
            text(
                """
                SELECT ae.asset_id,
                       ae.embedding_vector <=> CAST(:vec AS vector) AS distance
                FROM asset_embeddings ae
                JOIN assets a ON a.asset_id = ae.asset_id
                WHERE a.library_id   = :library_id
                  AND a.availability = 'online'
                  AND ae.model_id      = :model_id
                  AND ae.model_version = :model_version
                  AND ae.asset_id     != :exclude_id
                ORDER BY distance ASC
                LIMIT :limit OFFSET :offset
            """
            ),
            {
                "vec": str(vector),
                "library_id": library_id,
                "model_id": model_id,
                "model_version": model_version,
                "exclude_id": exclude_asset_id,
                "limit": limit,
                "offset": offset,
            },
        ).fetchall()
        return [(r.asset_id, float(r.distance)) for r in rows]


class WorkerJobRepository:
    """Repository for worker_jobs table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, job_id: str) -> WorkerJob | None:
        """Return job by id or None."""
        return self._session.get(WorkerJob, job_id)

    def create(self, job_type: str, asset_id: str) -> WorkerJob:
        """Create a pending job. job_id = job_ + ULID."""
        job_id = "job_" + str(ULID())
        job = WorkerJob(
            job_id=job_id,
            job_type=job_type,
            asset_id=asset_id,
            status="pending",
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
    ) -> int:
        """Count jobs with status pending or claimed. Same filters as claim_next (job_type, optional library_id)."""
        stmt = (
            select(func.count())
            .select_from(WorkerJob)
            .where(
                WorkerJob.job_type == job_type,
                WorkerJob.status.in_(["pending", "claimed"]),
            )
        )
        if library_id is not None:
            stmt = stmt.join(Asset, WorkerJob.asset_id == Asset.asset_id)
            stmt = stmt.where(Asset.library_id == library_id)
        result = self._session.execute(stmt)
        return int(result.scalar() or 0)

    def claim_next(
        self,
        job_type: str,
        worker_id: str,
        lease_minutes: int,
        library_id: str | None = None,
    ) -> WorkerJob | None:
        """Claim next pending (or expired claimed) job with FOR UPDATE SKIP LOCKED. Return None if none."""
        now = _utcnow()
        stmt = (
            select(WorkerJob)
            .where(WorkerJob.job_type == job_type)
            .where(
                or_(
                    WorkerJob.status == "pending",
                    and_(
                        WorkerJob.status == "claimed",
                        WorkerJob.lease_expires_at < now,
                    ),
                )
            )
        )
        if library_id is not None:
            stmt = stmt.join(Asset, WorkerJob.asset_id == Asset.asset_id)
            stmt = stmt.where(Asset.library_id == library_id)
        stmt = (
            stmt.order_by(WorkerJob.created_at)
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
        job.completed_at = _utcnow()
        job.error_message = None
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)

    def set_failed(self, job: WorkerJob, error_message: str) -> None:
        """Set job status to failed and error_message."""
        job.status = "failed"
        job.completed_at = _utcnow()
        job.error_message = error_message
        self._session.add(job)
        self._session.commit()
        self._session.refresh(job)

    def cancel_pending_for_assets(self, asset_ids: list[str], job_type: str) -> int:
        """Cancel pending/claimed jobs for given assets and job_type. Used by force enqueue."""
        if not asset_ids:
            return 0
        result = self._session.execute(
            text("""
                UPDATE worker_jobs
                SET status = 'cancelled'
                WHERE asset_id = ANY(:asset_ids)
                  AND job_type = :job_type
                  AND status IN ('pending', 'claimed')
            """),
            {"asset_ids": asset_ids, "job_type": job_type},
        )
        self._session.commit()
        return result.rowcount

    def cancel_failed_for_assets(self, asset_ids: list[str], job_type: str) -> int:
        """Cancel failed jobs for given assets and job_type. Used by retry_failed enqueue."""
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
            normalised = path_prefix.replace("\\", "/").strip().strip("/")
            path_filter = " AND (a.rel_path = :path_exact OR a.rel_path LIKE :path_pattern)"
            params["path_exact"] = normalised
            params["path_pattern"] = normalised + "/%"

        # Total count (distinct assets with failed jobs, with optional path filter).
        # DISTINCT ON (asset_id) is correct: job_type is in WHERE, so we get one row per asset
        # for this job type; multiple historical failed rows collapse to the latest per asset.
        count_sql = f"""
            SELECT COUNT(*)::int FROM (
                SELECT DISTINCT ON (wj.asset_id) wj.asset_id
                FROM worker_jobs wj
                JOIN assets a ON a.asset_id = wj.asset_id
                WHERE a.library_id = :library_id
                  AND wj.job_type = :job_type
                  AND wj.status = 'failed'
                  {path_filter}
                ORDER BY wj.asset_id, wj.created_at DESC
            ) sub
        """
        total = int(
            self._session.execute(text(count_sql), params).scalar() or 0
        )

        # Rows: most recent failed job per asset (DISTINCT ON correct: job_type in WHERE)
        params["limit"] = limit
        rows_sql = f"""
            SELECT * FROM (
                SELECT DISTINCT ON (wj.asset_id)
                    a.rel_path,
                    wj.error_message,
                    wj.completed_at
                FROM worker_jobs wj
                JOIN assets a ON a.asset_id = wj.asset_id
                WHERE a.library_id = :library_id
                  AND wj.job_type = :job_type
                  AND wj.status = 'failed'
                  {path_filter}
                ORDER BY wj.asset_id, wj.created_at DESC
            ) sub
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


class SearchSyncQueueRepository:
    """Repository for search_sync_queue outbox table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        asset_id: str,
        operation: str,
        scene_id: str | None = None,
    ) -> SearchSyncQueue:
        """
        Insert a new row into search_sync_queue.

        sync_id is generated as ssq_ + ULID().
        """
        sync = SearchSyncQueue(
            sync_id="ssq_" + str(ULID()),
            asset_id=asset_id,
            scene_id=scene_id,
            operation=operation,
            status="pending",
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
            normalised = path_prefix.replace("\\", "/").strip("/")
            path_filter = " AND a.rel_path LIKE :path_prefix"
            params["path_prefix"] = normalised + "/%"

        sql = f"""
            WITH candidates AS (
                SELECT ssl.sync_id, ssl.asset_id
                FROM search_sync_latest ssl
                JOIN assets a ON a.asset_id = ssl.asset_id
                WHERE ssl.status = 'pending'
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
            SET status = 'processing'
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
            SELECT asset_id FROM assets
            WHERE library_id = :library_id
            AND availability = 'online'
            AND status != 'trashed'
        """
        params: dict = {"library_id": library_id}
        if path_prefix:
            normalised = path_prefix.replace("\\", "/").strip("/")
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

            for aid in to_insert:
                self.enqueue(aid, "index", scene_id=None)

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
            normalised = path_prefix.replace("\\", "/").strip("/")
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
