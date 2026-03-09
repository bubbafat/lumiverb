"""Repository classes for the tenant database. All take session: Session in constructor."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session, select

from src.models.filter import AssetFilterSpec
from src.models.tenant import Asset, AssetMetadata, Library, Scan, SearchSyncQueue, WorkerJob
from ulid import ULID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    def find_similar(
        self,
        asset_id: str,
        library_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[tuple[Asset, float]]:
        """
        Find assets similar to the given asset_id using cosine distance
        on embedding_vector. Returns list of (Asset, distance) tuples,
        ordered by ascending distance (most similar first).

        Returns empty list if the source asset has no embedding.
        """
        source = self.get_by_id(asset_id)
        if source is None or source.embedding_vector is None:
            return []

        stmt = text(
            """
        SELECT
            asset_id,
            embedding_vector <=> CAST(:vec AS vector) AS distance
        FROM assets
        WHERE library_id = :library_id
          AND asset_id != :asset_id
          AND embedding_vector IS NOT NULL
          AND availability = 'online'
        ORDER BY distance ASC
        LIMIT :limit OFFSET :offset
        """
        )
        rows = self._session.execute(
            stmt,
            {
                "vec": str([float(x) for x in source.embedding_vector]),
                "library_id": library_id,
                "asset_id": asset_id,
                "limit": limit,
                "offset": offset,
            },
        ).fetchall()

        if not rows:
            return []

        result_ids = [r.asset_id for r in rows]
        distance_by_id = {r.asset_id: float(r.distance) for r in rows}
        assets_by_id = {a.asset_id: a for a in self.get_by_ids(result_ids)}

        return [
            (assets_by_id[aid], distance_by_id[aid])
            for aid in result_ids
            if aid in assets_by_id
        ]

    def set_embedding(self, asset_id: str, vector: list[float]) -> None:
        """Store the embedding vector for an asset."""
        self._session.execute(
            text(
                "UPDATE assets SET embedding_vector = CAST(:vec AS vector) "
                "WHERE asset_id = :asset_id"
            ),
            {"vec": str(vector), "asset_id": asset_id},
        )
        self._session.commit()

    def query_for_enqueue(
        self,
        filter: AssetFilterSpec,
        job_type: str,
        force: bool,
    ) -> list[str]:
        """
        Return asset_ids matching filter spec, suitable for job enqueueing.

        If force=False: excludes assets that already have a pending/claimed
        job of this job_type, and excludes assets where proxy_key/thumbnail_key
        is already set (for proxy/thumbnail job types).

        If force=True: returns all matching assets regardless of existing jobs.

        Resolution order: if asset_id is set, all other filters ignored.
        """
        conditions = ["a.library_id = :library_id"]
        params: dict = {"library_id": filter.library_id, "job_type": job_type}

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
                          AND m.model_id = 'moondream'
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

        if not force:
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
                          AND m.model_id = 'moondream'
                    )
                    """
                )

        where = " AND ".join(conditions)
        sql = f"SELECT a.asset_id FROM assets a WHERE {where} ORDER BY a.asset_id"
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

    def claim_batch(self, limit: int) -> list[SearchSyncQueue]:
        """
        Claim up to `limit` pending rows using FOR UPDATE SKIP LOCKED.

        Returns the claimed rows with status updated to 'processing'.
        """
        stmt = (
            select(SearchSyncQueue)
            .where(SearchSyncQueue.status == "pending")
            .order_by(SearchSyncQueue.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list(self._session.exec(stmt).all())
        if not rows:
            return []
        now = _utcnow()
        for row in rows:
            row.status = "processing"
            # created_at is immutable; updated_at column does not exist on this table
            # so we do not touch timestamps here.
            self._session.add(row)
        self._session.commit()
        return rows

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
