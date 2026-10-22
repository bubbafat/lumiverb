"""The daily sweep: the stored files no row names, removed (ADR-020: through
the Store, by key, one library at a time).

Safety guards:
- DB query failures skip the affected tenant/library (never treat empty results as "nothing expected")
- Files newer than 1 hour are skipped (may be mid-ingest)
- If >25% of files in a library would be deleted, abort that library
- A library whose storage can't be reached (a drive unplugged) is skipped, and
  says so: nothing there is taken for missing or orphaned
- Library and account folders the database doesn't list are only reported
  (orphan_folders), never removed here: a database restored from an older
  backup looks just like that (Robert). remove_orphan_folders removes them,
  when an admin names them or says all.
- Waits while the previews move
- Dry-run by default

Folders, and the partial writes a crash left, are the filesystem provider's:
src/server/storage/folders.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlmodel import Session

from src.server.storage import folders
from src.server.storage import store as stored

logger = logging.getLogger(__name__)

# Files must be older than this (seconds) to be eligible for deletion.
_MIN_AGE_SECONDS = 3600  # 1 hour
# If more than this fraction of files in a library would be deleted, skip it.
_MAX_DELETE_FRACTION = 0.25

MOVING = folders.MOVING


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
) -> set[str] | None:
    """Return the set of all artifact keys for a library (including trashed assets).

    Returns None on DB error (caller should skip this library).

    Includes:
      - assets.{proxy_key, thumbnail_key, video_preview_key, analysis_proxy_key}
      - video_scenes.{proxy_key, thumbnail_key} (per-scene WebP artifacts)
      - scene_rep JPG paths derived from (tenant, library, asset_id, rep_frame_ms)
        — these are NOT stored in any column; they live at the deterministic
        path returned by store.scene_rep_key(). Without computing them here,
        cleanup would flag every scene_rep JPG on disk as orphaned.
      - each face's crop: faces.crop_key, and the key store.face_crop_key()
        gives it (a crop written before its row says so is still its face's).
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
                stored.scene_rep_key(tenant_id, library_id, asset_id, rep_frame_ms)
            )

        for face_id, crop_key in session.execute(text("""
            SELECT f.face_id, f.crop_key
            FROM faces f
            JOIN assets a ON a.asset_id = f.asset_id
            WHERE a.library_id = :lib_id
        """), {"lib_id": library_id}).fetchall():
            keys.update(k for k in (crop_key, stored.face_crop_key_of(tenant_id, library_id, face_id)) if k)

        return keys
    except Exception as exc:
        logger.error("Failed to query expected keys for library %s: %s", library_id, exc)
        return None


def _skipped(result: CleanupResult, why: str) -> CleanupResult:
    result.skipped_libraries += 1
    result.errors.append(why)
    return result


def _sweep_library(
    store: stored.Store,
    tenant_id: str,
    library_id: str,
    expected: set[str],
    *,
    dry_run: bool,
) -> CleanupResult:
    """One library's stored files no row names, removed: those an hour old
    or more, unless they're over a quarter of the library's files."""
    result = CleanupResult()
    unreachable = (f"Skipped library {library_id}: its previews can't be reached now "
                   "(is its drive plugged in?)")
    now = time.time()
    try:
        keys = [key for key in store.list(f"{tenant_id}/{library_id}/") if stored.kind_of(key)]
        orphans: list[tuple[str, int]] = []
        for key in keys:
            if key in expected:
                continue
            try:
                st = store.stat(key)
            except (stored.NotFound, stored.UnsafeKeyError):
                continue  # gone meanwhile, or a symbolic link since: never followed (the listing passes them by)
            if now - st.modified < _MIN_AGE_SECONDS:
                continue  # may be mid-ingest
            orphans.append((key, st.size))
    except stored.Unavailable:
        logger.warning("cleanup: %s", unreachable)
        return _skipped(result, unreachable)

    # Safety check: abort if too many files would be deleted
    if len(orphans) > _MAX_DELETE_FRACTION * len(keys):
        msg = (
            f"Skipped library {library_id}: {len(orphans)}/{len(keys)} "
            f"files ({len(orphans) / len(keys):.0%}) would be deleted, "
            f"exceeds {_MAX_DELETE_FRACTION:.0%} safety threshold"
        )
        logger.warning(msg)
        return _skipped(result, msg)

    for key, size in orphans:
        logger.info("%s orphan file: %s (%d bytes)", "Would remove" if dry_run else "Removing", key, size)
        if not dry_run:
            try:
                store.delete(key)
            except stored.Unavailable:
                logger.warning("cleanup: %s", unreachable)
                result.errors.append(unreachable)
                break
        result.orphan_files += 1
        result.bytes_freed += size
    return result


def _merged(result: CleanupResult, other: CleanupResult) -> None:
    result.orphan_folders.extend(other.orphan_folders)
    result.orphan_files += other.orphan_files
    result.bytes_freed += other.bytes_freed
    result.skipped_libraries += other.skipped_libraries
    result.errors.extend(other.errors)


def run_cleanup_for_tenant(
    tenant_id: str,
    session: Session,
    *,
    dry_run: bool = True,
    library_id: str | None = None,
) -> CleanupResult:
    """Run file cleanup for a single tenant. Session must be for the tenant DB.

    library_id: only that library; the account's folders the database
    doesn't list and its playback cuts (not a library's) are left for a
    whole-account run.
    """
    result = CleanupResult()

    # Every library in the DB, trashed ones too: they still have files.
    try:
        known = {r[0] for r in session.execute(text("SELECT library_id FROM libraries")).fetchall()}
    except Exception as exc:
        result.errors.append(f"Failed to query libraries for tenant {tenant_id}: {exc}")
        return result

    store = stored.get_store()
    swept = sorted(known if library_id is None else known & {library_id})
    for lib in swept:
        expected_keys = _get_expected_keys_for_library(session, lib, tenant_id)
        if expected_keys is None:
            _skipped(result, f"Skipped library {lib}: DB query failed")
            continue
        _merged(result, _sweep_library(store, tenant_id, lib, expected_keys, dry_run=dry_run))

    crashed, crashed_bytes = folders.left_by_a_crash(tenant_id, swept, dry_run=dry_run)
    result.orphan_files += crashed
    result.bytes_freed += crashed_bytes

    if library_id is not None:
        return result

    result.orphan_folders.extend(folders.measured(f"{tenant_id}/{lib}")
                                 for lib in sorted(folders.libraries(tenant_id) - known))

    # Playback cuts (copies of each video's start) of assets that are gone.
    try:
        from src.server.api.routers.playback import sweep_cuts
        from src.server.storage.cache import playback_cuts

        known_assets = {r[0] for r in session.execute(text("SELECT asset_id FROM assets")).fetchall()}
        result.orphan_files += sweep_cuts(playback_cuts(tenant_id), known_assets, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — never fail the rest of cleanup over a cache
        result.errors.append(f"Playback cut sweep failed for tenant {tenant_id}: {exc}")

    return result


def run_cleanup_all_tenants(*, dry_run: bool = True) -> CleanupResult:
    """Run cleanup across all tenants (the control plane's), and report the
    folders of accounts it doesn't list."""
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository

    if why_not := folders.why_waiting():
        return CleanupResult(errors=[why_not])

    with get_control_session() as control_session:
        tenant_ids = sorted(t.tenant_id for t in TenantRepository(control_session).list_all())

    result = CleanupResult()
    result.orphan_folders.extend(folders.measured(t) for t in sorted(folders.accounts() - set(tenant_ids)))

    for tenant_id in tenant_ids:
        try:
            with get_tenant_session(tenant_id) as session:
                if _paused(session, tenant_id, dry_run):
                    result.paused = True
                    result.paused_tenants.append(tenant_id)
                    continue
                _merged(result, run_cleanup_for_tenant(tenant_id, session, dry_run=dry_run))
        except Exception as exc:
            logger.error("Failed to run cleanup for tenant %s: %s", tenant_id, exc)
            result.errors.append(f"Tenant {tenant_id}: {exc}")

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
    if why_not := folders.why_waiting():
        return CleanupResult(errors=[why_not])
    if _paused(session, tenant_id, dry_run):
        return CleanupResult(paused=True, paused_tenants=[tenant_id])
    result = run_cleanup_for_tenant(tenant_id, session, dry_run=dry_run, library_id=library_id)
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


def _listed(session: Session, library_id: str) -> bool:
    return session.execute(
        text("SELECT 1 FROM libraries WHERE library_id = :id"), {"id": library_id},
    ).first() is not None


def remove_orphan_folders(
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
    Waits, removing nothing, while the previews move.
    """
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository

    if why_not := folders.why_waiting():
        return RemovedFolders(errors=[why_not])
    wanted = None if folder_ids is None else set(folder_ids)
    out = RemovedFolders()

    def take(name: str) -> bool:
        return wanted is None or name in wanted

    def remove(prefix: str, still_unlisted) -> None:
        try:
            folder = folders.remove(prefix, still_unlisted, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — the other folders go on
            logger.error("remove-orphan-folders failed for %s: %s", prefix, exc)
            out.errors.append(f"{prefix}: {exc}")
            return
        if folder is not None:
            out.removed.append(folder)
            out.bytes_freed += folder["bytes"]

    def libraries_of(tid: str, sess: Session) -> None:
        known = {r[0] for r in sess.execute(text("SELECT library_id FROM libraries")).fetchall()}
        for name in sorted(folders.libraries(tid) - known):
            if take(name):
                remove(f"{tid}/{name}", lambda n=name: not _listed(sess, n))

    if tenant_id is not None:
        libraries_of(tenant_id, session)
    else:
        def tenant_ids() -> set[str]:
            with get_control_session() as ctrl:
                return {t.tenant_id for t in TenantRepository(ctrl).list_all()}

        known_tenants = tenant_ids()
        on_disk = folders.accounts()
        for name in sorted(on_disk - known_tenants):
            if take(name):
                remove(name, lambda n=name: n not in tenant_ids())
        for name in sorted(on_disk & known_tenants):
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
