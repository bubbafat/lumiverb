"""Filesystem cleanup: detect and remove orphaned tenant dirs, library dirs, and artifact files.

Safety guards:
- DB query failures skip the affected tenant/library (never treat empty results as "nothing expected")
- Files newer than 1 hour are skipped (may be mid-ingest)
- If >25% of files in a library would be deleted, abort that library
- Library and account folders the database doesn't list are only reported
  (orphan_folders), never removed here: a database restored from an older
  backup looks just like that (Robert). remove_orphan_folders removes them,
  when an admin names them or says all.
- Dry-run by default
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
_ARTIFACT_SUBDIRS = ("proxies", "thumbnails", "previews", "scenes", "analysis")


@dataclass
class CleanupResult:
    """Aggregated cleanup result across all tenants."""
    orphan_files: int = 0
    # Library and account folders the database doesn't list: reported, kept.
    orphan_folders: list[dict] = field(default_factory=list)
    bytes_freed: int = 0
    skipped_libraries: int = 0
    errors: list[str] = field(default_factory=list)
    # Skipped because Upkeep is paused (not "nothing to clean up"), and which accounts.
    paused: bool = False
    paused_tenants: list[str] = field(default_factory=list)


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


def _folder(data_dir: Path, path: Path) -> dict:
    """A folder the database doesn't list, as reported: its id (the folder's
    name), its path under DATA_DIR, its size and its newest mtime."""
    size = 0
    newest = 0.0
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files, ""]:
            try:
                st = (Path(root) / name).stat()
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if name in files:
                size += st.st_size
    return {
        "folder_id": path.name,
        "path": str(path.relative_to(data_dir)),
        "bytes": size,
        "newest_mtime": datetime.fromtimestamp(newest, tz=UTC).isoformat() if newest else None,
    }


def _warn_orphan_folders(result: CleanupResult) -> None:
    """One warning per run naming the folders kept."""
    if result.orphan_folders:
        logger.warning(
            "cleanup: %d folder(s) the database doesn't list, kept (lumiverb maintenance remove-orphan-folders): %s",
            len(result.orphan_folders), ", ".join(f["path"] for f in result.orphan_folders),
        )


def _get_expected_keys_for_library(
    session: Session,
    library_id: str,
    tenant_id: str,
    storage: LocalStorage,
) -> set[str] | None:
    """Return the set of all artifact keys for a library (including trashed assets).

    Returns None on DB error (caller should skip this library).

    Includes:
      - assets.{proxy_key, thumbnail_key, video_preview_key, analysis_proxy_key}
      - video_scenes.{proxy_key, thumbnail_key} (per-scene WebP artifacts)
      - scene_rep JPG paths derived from (tenant, library, asset_id, rep_frame_ms)
        — these are NOT stored in any column; they live at the deterministic
        path returned by storage.scene_rep_key(). Without computing them here,
        cleanup would flag every scene_rep JPG on disk as orphaned.
    """
    try:
        # Asset artifacts: proxy_key, thumbnail_key, video_preview_key, analysis_proxy_key
        rows = session.execute(text("""
            SELECT proxy_key, thumbnail_key, video_preview_key, analysis_proxy_key
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
    library_id: str | None = None,
) -> CleanupResult:
    """Run file cleanup for a single tenant. Session must be for the tenant DB.

    library_id: only that library's directory; the account's playback cuts
    (not a library's) are left for a whole-account run.
    """
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
        d for d in _list_subdirs(tenant_dir)
        if d.startswith("lib_") and (library_id is None or d == library_id)
    ]

    for lib_dir_name in disk_lib_dirs:
        lib_dir = tenant_dir / lib_dir_name

        if lib_dir_name not in known_library_ids:
            result.orphan_folders.append(_folder(data_dir, lib_dir))
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

    if library_id is not None:
        return result

    # Playback cuts (copies of each video's start) of assets that are gone.
    try:
        from src.server.api.routers.playback import sweep_cuts

        known_assets = {r[0] for r in session.execute(text("SELECT asset_id FROM assets")).fetchall()}
        result.orphan_files += sweep_cuts(tenant_dir / "playback", known_assets, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — never fail the rest of cleanup over a cache
        result.errors.append(f"Playback cut sweep failed for tenant {tenant_id}: {exc}")

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
            result.orphan_folders.append(_folder(data_dir, tenant_dir))
            continue

        # Tenant exists — check its libraries and files
        try:
            with get_tenant_session(tenant_dir_name) as session:
                if _paused(session, tenant_dir_name, dry_run):
                    result.paused = True
                    result.paused_tenants.append(tenant_dir_name)
                    continue
                tenant_result = run_cleanup_for_tenant(
                    data_dir, tenant_dir_name, session, dry_run=dry_run,
                )
        except Exception as exc:
            logger.error("Failed to run cleanup for tenant %s: %s", tenant_dir_name, exc)
            result.errors.append(f"Tenant {tenant_dir_name}: {exc}")
            continue

        result.orphan_folders.extend(tenant_result.orphan_folders)
        result.orphan_files += tenant_result.orphan_files
        result.bytes_freed += tenant_result.bytes_freed
        result.skipped_libraries += tenant_result.skipped_libraries
        result.errors.extend(tenant_result.errors)

    _warn_orphan_folders(result)
    return result


def run_cleanup_single_tenant(
    tenant_id: str,
    session: Session,
    *,
    dry_run: bool = True,
    library_id: str | None = None,
) -> CleanupResult:
    """Run cleanup for a single tenant (used when called with tenant API key),
    or one library of it."""
    from src.server.config import get_settings

    data_dir = Path(get_settings().data_dir)
    if not data_dir.is_dir():
        return CleanupResult(errors=[f"Data dir does not exist: {data_dir}"])
    if _paused(session, tenant_id, dry_run):
        return CleanupResult(paused=True, paused_tenants=[tenant_id])
    result = run_cleanup_for_tenant(data_dir, tenant_id, session, dry_run=dry_run, library_id=library_id)
    _warn_orphan_folders(result)
    return result


def _paused(session: Session, tenant_id: str, dry_run: bool) -> bool:
    """An admin paused the account's Upkeep switch (Robert, Oct 9): its files
    aren't cleaned up meanwhile; a dry run still reports."""
    from src.server.repository import lineage

    if dry_run or not lineage.upkeep_paused(session):
        return False
    logger.info("cleanup: %s is paused; nothing deleted", tenant_id)
    return True


@dataclass
class RemovedFolders:
    removed: list[dict] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)


def _remove(data_dir: Path, path: Path, still_unlisted, dry_run: bool, out: RemovedFolders) -> None:
    """Remove one folder after checking again that the database doesn't list it."""
    if not still_unlisted():
        logger.info("remove-orphan-folders: %s is listed now; kept", path)
        return
    folder = _folder(data_dir, path)
    logger.warning("remove-orphan-folders: %s %s (%d bytes)", "would remove" if dry_run else "removing",
                   path, folder["bytes"])
    if not dry_run:
        import shutil

        shutil.rmtree(path)
    out.removed.append(folder)
    out.bytes_freed += folder["bytes"]


def _listed(session: Session, library_id: str) -> bool:
    return session.execute(
        text("SELECT 1 FROM libraries WHERE library_id = :id"), {"id": library_id},
    ).first() is not None


def remove_orphan_folders(
    data_dir: Path,
    *,
    tenant_id: str | None,
    session: Session | None,
    folder_ids: list[str] | None,
    dry_run: bool,
) -> RemovedFolders:
    """Remove library folders (and, for every account: tenant_id None,
    account folders) the database doesn't list: those in folder_ids, or all
    (None). Each is checked against the database again just before it goes.

    tenant_id with its session: that account's library folders. tenant_id
    None: every account's, and the folders of accounts that are gone.
    """
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository

    wanted = None if folder_ids is None else set(folder_ids)
    out = RemovedFolders()

    def take(name: str) -> bool:
        return wanted is None or name in wanted

    def libraries_of(tid: str, sess: Session) -> None:
        known = {r[0] for r in sess.execute(text("SELECT library_id FROM libraries")).fetchall()}
        for name in _list_subdirs(data_dir / tid):
            if name.startswith("lib_") and name not in known and take(name):
                _remove(data_dir, data_dir / tid / name, lambda n=name: not _listed(sess, n), dry_run, out)

    if tenant_id is not None:
        libraries_of(tenant_id, session)
    else:
        def tenant_ids() -> set[str]:
            with get_control_session() as ctrl:
                return {t.tenant_id for t in TenantRepository(ctrl).list_all()}

        known_tenants = tenant_ids()
        for name in _list_subdirs(data_dir):
            if not name.startswith("ten_"):
                continue
            if name not in known_tenants:
                if take(name):
                    _remove(data_dir, data_dir / name, lambda n=name: n not in tenant_ids(), dry_run, out)
                continue
            try:
                with get_tenant_session(name) as sess:
                    libraries_of(name, sess)
            except Exception as exc:  # noqa: BLE001 — the other accounts go on
                logger.error("remove-orphan-folders failed for tenant %s: %s", name, exc)
                out.errors.append(f"Tenant {name}: {exc}")

    if wanted is not None:
        done = {f["folder_id"] for f in out.removed}
        out.not_found = sorted(wanted - done)
    return out
