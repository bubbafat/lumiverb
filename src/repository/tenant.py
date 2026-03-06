"""Repository classes for the tenant database. All take session: Session in constructor."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from src.models.tenant import Asset, Library, Scan
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

    def list_all(self) -> list[Library]:
        """Return all libraries."""
        stmt = select(Library)
        return list(self._session.exec(stmt).all())

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
