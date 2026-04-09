"""Filesystem cleanup: detect and remove orphaned tenant dirs, library dirs, and artifact files.

Safety guards:
- DB query failures skip the affected tenant/library (never treat empty results as "nothing expected")
- Files newer than 1 hour are skipped (may be mid-ingest)
- If >25% of files in a library would be deleted, abort that library
- Dry-run by default
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session

from src.server.storage.local import LocalStorage

logger = logging.getLogger(__name__)

# Files must be older than this (seconds) to be eligible for deletion.
_MIN_AGE_SECONDS = 3600  # 1 hour
# If more than this fraction of files in a library would be deleted, skip it.
_MAX_DELETE_FRACTION = 0.25

# Subdirectories under a library dir that contain artifacts.
_ARTIFACT_SUBDIRS = ("proxies", "thumbnails", "previews", "scenes")


@dataclass
class CleanupResult:
    """Aggregated cleanup result across all tenants."""
    orphan_tenants: int = 0
    orphan_libraries: int = 0
    orphan_files: int = 0
    bytes_freed: int = 0
    skipped_libraries: int = 0
    errors: list[str] = field(default_factory=list)


def _list_subdirs(parent: Path) -> list[str]:
    """Return names of immediate subdirectories."""
    if not parent.is_dir():
        return []
    return [d.name for d in parent.iterdir() if d.is_dir()]


def _walk_files(directory: Path) -> list[Path]:
    """Recursively list all files under directory."""
    files = []
    if not directory.is_dir():
        return files
    for root, _dirs, filenames in os.walk(directory):
        for name in filenames:
            files.append(Path(root) / name)
    return files


def _file_age_seconds(path: Path) -> float:
    """Return file age in seconds based on mtime."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return 0.0


def _rmtree(path: Path, dry_run: bool) -> int:
    """Remove a directory tree. Returns total bytes of files removed."""
    total = 0
    for f in _walk_files(path):
        total += f.stat().st_size
    if not dry_run:
        import shutil
        shutil.rmtree(path)
    return total


def _get_expected_keys_for_library(
    session: Session,
    library_id: str,
    tenant_id: str,
    storage: LocalStorage,
) -> set[str] | None:
    """Return the set of all artifact keys for a library (including trashed assets).

    Returns None on DB error (caller should skip this library).

    Includes:
      - assets.{proxy_key, thumbnail_key, video_preview_key}
      - video_scenes.{proxy_key, thumbnail_key} (per-scene WebP artifacts)
      - scene_rep JPG paths derived from (tenant, library, asset_id, rep_frame_ms)
        — these are NOT stored in any column; they live at the deterministic
        path returned by storage.scene_rep_key(). Without computing them here,
        cleanup would flag every scene_rep JPG on disk as orphaned.
    """
    try:
        # Asset artifacts: proxy_key, thumbnail_key, video_preview_key
        rows = session.execute(text("""
            SELECT proxy_key, thumbnail_key, video_preview_key
            FROM assets
            WHERE library_id = :lib_id
        """), {"lib_id": library_id}).fetchall()

        keys: set[str] = set()
        for row in rows:
            for val in row:
                if val:
                    keys.add(val)

        # Scene-level artifacts. video_scenes.proxy_key/thumbnail_key are the
        # per-scene WebP previews (often null). The scene_rep JPG path is
        # derived from rep_frame_ms and must be computed here.
        scene_rows = session.execute(text("""
            SELECT vs.asset_id, vs.rep_frame_ms, vs.proxy_key, vs.thumbnail_key
            FROM video_scenes vs
            JOIN assets a ON a.asset_id = vs.asset_id
            WHERE a.library_id = :lib_id
        """), {"lib_id": library_id}).fetchall()

        for row in scene_rows:
            asset_id = row[0]
            rep_frame_ms = row[1]
            scene_proxy_key = row[2]
            scene_thumb_key = row[3]
            if scene_proxy_key:
                keys.add(scene_proxy_key)
            if scene_thumb_key:
                keys.add(scene_thumb_key)
            keys.add(
                storage.scene_rep_key(tenant_id, library_id, asset_id, rep_frame_ms)
            )

        return keys
    except Exception as exc:
        logger.error("Failed to query expected keys for library %s: %s", library_id, exc)
        return None


def run_cleanup_for_tenant(
    data_dir: Path,
    tenant_id: str,
    session: Session,
    *,
    dry_run: bool = True,
) -> CleanupResult:
    """Run file cleanup for a single tenant. Session must be for the tenant DB."""
    result = CleanupResult()
    tenant_dir = data_dir / tenant_id
    storage = LocalStorage(str(data_dir))

    if not tenant_dir.is_dir():
        return result

    # Get all library IDs from DB (include trashed — they still have files)
    try:
        lib_rows = session.execute(
            text("SELECT library_id FROM libraries")
        ).fetchall()
        known_library_ids = {r[0] for r in lib_rows}
    except Exception as exc:
        result.errors.append(f"Failed to query libraries for tenant {tenant_id}: {exc}")
        return result

    # Check library dirs on disk
    disk_lib_dirs = [
        d for d in _list_subdirs(tenant_dir) if d.startswith("lib_")
    ]

    for lib_dir_name in disk_lib_dirs:
        lib_dir = tenant_dir / lib_dir_name

        if lib_dir_name not in known_library_ids:
            # Orphan library directory
            logger.info(
                "%s orphan library dir: %s",
                "Would remove" if dry_run else "Removing",
                lib_dir,
            )
            bytes_freed = _rmtree(lib_dir, dry_run)
            result.orphan_libraries += 1
            result.bytes_freed += bytes_freed
            continue

        # Library exists in DB — check individual files
        expected_keys = _get_expected_keys_for_library(
            session, lib_dir_name, tenant_id, storage,
        )
        if expected_keys is None:
            result.skipped_libraries += 1
            result.errors.append(f"Skipped library {lib_dir_name}: DB query failed")
            continue

        # Build set of relative keys for files on disk
        # Key format: {tenant_id}/{library_id}/proxies/{bucket}/{filename}
        disk_files: list[tuple[Path, str]] = []  # (abs_path, key)
        for subdir_name in _ARTIFACT_SUBDIRS:
            subdir = lib_dir / subdir_name
            for f in _walk_files(subdir):
                rel_key = str(f.relative_to(data_dir))
                disk_files.append((f, rel_key))

        if not disk_files:
            continue

        # Find orphans (on disk but not in DB)
        orphan_files: list[tuple[Path, int]] = []
        for abs_path, key in disk_files:
            if key not in expected_keys:
                # Skip files newer than threshold (may be mid-ingest)
                if _file_age_seconds(abs_path) < _MIN_AGE_SECONDS:
                    continue
                try:
                    size = abs_path.stat().st_size
                except OSError:
                    continue
                orphan_files.append((abs_path, size))

        # Safety check: abort if too many files would be deleted
        if len(orphan_files) > _MAX_DELETE_FRACTION * len(disk_files):
            msg = (
                f"Skipped library {lib_dir_name}: {len(orphan_files)}/{len(disk_files)} "
                f"files ({len(orphan_files) / len(disk_files):.0%}) would be deleted, "
                f"exceeds {_MAX_DELETE_FRACTION:.0%} safety threshold"
            )
            logger.warning(msg)
            result.skipped_libraries += 1
            result.errors.append(msg)
            continue

        for abs_path, size in orphan_files:
            logger.info(
                "%s orphan file: %s (%d bytes)",
                "Would remove" if dry_run else "Removing",
                abs_path,
                size,
            )
            if not dry_run:
                abs_path.unlink(missing_ok=True)
            result.orphan_files += 1
            result.bytes_freed += size

    return result


def run_cleanup_all_tenants(*, dry_run: bool = True) -> CleanupResult:
    """Run cleanup across all tenants. Uses control plane to enumerate tenants."""
    from src.server.config import get_settings
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository

    settings = get_settings()
    data_dir = Path(settings.data_dir)

    if not data_dir.is_dir():
        return CleanupResult(errors=[f"Data dir does not exist: {data_dir}"])

    result = CleanupResult()

    # Get known tenants from control plane
    with get_control_session() as control_session:
        tenants = TenantRepository(control_session).list_all()
    known_tenant_ids = {t.tenant_id for t in tenants}

    # Check tenant dirs on disk
    disk_tenant_dirs = [
        d for d in _list_subdirs(data_dir) if d.startswith("ten_")
    ]

    for tenant_dir_name in disk_tenant_dirs:
        tenant_dir = data_dir / tenant_dir_name

        if tenant_dir_name not in known_tenant_ids:
            # Orphan tenant directory
            logger.info(
                "%s orphan tenant dir: %s",
                "Would remove" if dry_run else "Removing",
                tenant_dir,
            )
            bytes_freed = _rmtree(tenant_dir, dry_run)
            result.orphan_tenants += 1
            result.bytes_freed += bytes_freed
            continue

        # Tenant exists — check its libraries and files
        try:
            with get_tenant_session(tenant_dir_name) as session:
                tenant_result = run_cleanup_for_tenant(
                    data_dir, tenant_dir_name, session, dry_run=dry_run,
                )
        except Exception as exc:
            logger.error("Failed to run cleanup for tenant %s: %s", tenant_dir_name, exc)
            result.errors.append(f"Tenant {tenant_dir_name}: {exc}")
            continue

        result.orphan_libraries += tenant_result.orphan_libraries
        result.orphan_files += tenant_result.orphan_files
        result.bytes_freed += tenant_result.bytes_freed
        result.skipped_libraries += tenant_result.skipped_libraries
        result.errors.extend(tenant_result.errors)

    return result


def run_cleanup_single_tenant(
    tenant_id: str,
    session: Session,
    *,
    dry_run: bool = True,
) -> CleanupResult:
    """Run cleanup for a single tenant (used when called with tenant API key)."""
    from src.server.config import get_settings

    data_dir = Path(get_settings().data_dir)
    if not data_dir.is_dir():
        return CleanupResult(errors=[f"Data dir does not exist: {data_dir}"])

    return run_cleanup_for_tenant(data_dir, tenant_id, session, dry_run=dry_run)
