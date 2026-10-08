"""Trash API: empty trash (permanent delete). Admin only."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.routers.archive import HiddenClip, decode_cursor, encode_cursor
from src.server.api.errors import DecisionRequiredError
from src.shared.utils import utcnow
from src.server.models.tenant import Asset
from src.server.repository.tenant import AssetRepository, LibraryRepository
from src.server.storage.local import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/trash", tags=["trash"])


class EmptyTrashRequest(BaseModel):
    asset_ids: list[str] | None = None
    trashed_before: str | None = None  # ISO8601
    # Only this library's trash, and only under this folder: what a filtered view showed.
    library_id: str | None = None
    path: str | None = None
    # Required when any of the clips are in projects: deleting them for good
    # takes them out of those projects, and the user has to have said yes.
    remove_from_projects: bool = False


class EmptyTrashResponse(BaseModel):
    deleted: int


class TrashedClip(HiddenClip):
    trashed_at: str
    # When it's deleted for good; None when the trash days are off.
    expires_at: str | None


class TrashPage(BaseModel):
    items: list[TrashedClip]
    next_cursor: str | None = None
    total: int
    # The account's trash days; None when the trash is emptied only by hand.
    trash_days: int | None


@router.get("", response_model=TrashPage, dependencies=[Depends(require_signed_in)])
def list_trash(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    path: str | None = Query(default=None, description="Only clips under this folder (recursive)."),
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> TrashPage:
    """Clips a person trashed, most recently trashed first, with when each is
    deleted for good. Clips of a library in the trash go with the library,
    so they're not here."""
    from datetime import timedelta

    from src.server.tenant_settings import get_trash_days

    days = get_trash_days(session)
    rows, total = AssetRepository(session).page_hidden(
        ("user",), library_id=library_id, folder=path, after=decode_cursor(after), limit=limit,
    )
    return TrashPage(
        items=[
            TrashedClip(
                asset_id=r["asset_id"], library_id=r["library_id"], library_name=r["library_name"],
                rel_path=r["rel_path"], media_type=r["media_type"], trashed_at=r["deleted_at"].isoformat(),
                expires_at=(r["deleted_at"] + timedelta(days=days)).isoformat() if days else None,
            )
            for r in rows
        ],
        next_cursor=encode_cursor(rows[-1]) if len(rows) == limit else None,
        total=total,
        trash_days=days,
    )


@router.delete("/empty", response_model=EmptyTrashResponse)
def empty_trash(
    request: Request,
    body: EmptyTrashRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> EmptyTrashResponse:
    """
    Permanently delete trashed assets. Admin only.
    Scope by asset_ids and/or trashed_before. If neither provided, delete all trashed.
    Deletes DB rows in FK-safe order, then best-effort file and Quickwit cleanup.
    """
    asset_repo = AssetRepository(session)
    trashed_before_dt: datetime | None = None
    if body.trashed_before:
        try:
            trashed_before_dt = datetime.fromisoformat(
                body.trashed_before.replace("Z", "+00:00")
            )
        except ValueError:
            trashed_before_dt = None
    if body.asset_ids is None and trashed_before_dt is None:
        trashed_before_dt = utcnow()
    to_delete = asset_repo.list_trashed(asset_ids=body.asset_ids, trashed_before=trashed_before_dt,
                                        library_id=body.library_id, folder=body.path)
    if not to_delete:
        return EmptyTrashResponse(deleted=0)
    return EmptyTrashResponse(
        deleted=purge_assets(session, getattr(request.state, "tenant_id", None), to_delete, user_id,
                             remove_from_projects=body.remove_from_projects),
    )


def hand_over_to_copies(session: Session, request: Request, archived_ids: list[str]) -> list[str]:
    """Copy, then delete the original (when the account follows moves): each of
    these just-archived assets whose content has an empty, newer copy in the
    library moves to that copy's path, with its notes, ratings, projects and
    people, and the copy goes. Returns the ids that moved.

    The archive has already committed: a handover that fails is logged and
    rolled back, never a failed request (the scan would stop, and the original,
    no longer listed, would never be tried again)."""
    moved: list[str] = []
    libraries: set[str] = set()
    for asset_id in archived_ids:
        try:
            library_id = _hand_over(session, request, asset_id)
            if library_id:
                moved.append(asset_id)
                libraries.add(library_id)
        except Exception as exc:  # noqa: BLE001 — the asset stays archived, as without moves
            session.rollback()
            now = session.get(Asset, asset_id)
            if now is not None and now.deleted_at is None:
                # The move committed; a step after it failed.
                logger.warning("Asset %s moved to its copy, but a step after failed: %s", asset_id, exc)
                moved.append(asset_id)
                libraries.add(now.library_id)
            elif _lock_timeout(exc):
                logger.info("Asset %s stays archived: its copy was busy (a person working on it)", asset_id)
            else:
                logger.exception("Couldn't move asset %s to its copy; it stays archived", asset_id)
    for library_id in libraries:  # open grids drop the copies' tiles
        try:
            LibraryRepository(session).bump_revision(library_id)
        except Exception as exc:  # noqa: BLE001 — the grid catches up on its next change
            logger.warning("Couldn't bump library %s's revision after a handover: %s", library_id, exc)
    return moved


def _lock_timeout(exc: Exception) -> bool:
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "55P03"  # lock_not_available


def _hand_over(session: Session, request: Request, asset_id: str) -> str | None:
    """Move one archived asset to its empty copy. Returns its library when it moved."""
    repo = AssetRepository(session)
    archived = session.get(Asset, asset_id)  # get_by_id leaves out deleted assets
    if (archived is None or not repo.lock_for_restore(archived, archived.rel_path)
            or archived.deleted_at is None or archived.deleted_reason != "missing"):
        session.rollback()
        return None
    # A person un-assigning a face or merging people locks in the other order
    # (people or matches, then faces): give way within 200 ms, before Postgres
    # would call it a deadlock and fail their request.
    session.execute(text("SET LOCAL lock_timeout = '200ms'"))
    copy = repo.find_empty_newer_copy(archived)
    if copy is None:
        session.rollback()
        return None
    path, copy_id, library_id = copy.rel_path, copy.asset_id, archived.library_id
    # Out of the way first: (library, rel_path) is unique.
    copy.rel_path = f".lumiverb-handed-over/{copy_id}"
    copy.deleted_at = utcnow()
    copy.deleted_reason = "handed_over"
    session.add(copy)
    session.flush()
    archived.rel_path = path
    archived.file_size = copy.file_size
    archived.file_mtime = copy.file_mtime
    archived.updated_at = utcnow()
    repo.clear_trash(archived)
    session.add(archived)
    # A cover someone set to the copy follows the asset.
    for table in ("libraries", "projects"):
        session.execute(text(f"UPDATE {table} SET cover_asset_id = :a WHERE cover_asset_id = :c"),
                        {"a": asset_id, "c": copy_id})
    session.flush()
    # Deletes the copy for good and commits the move with it.
    if purge_assets(session, getattr(request.state, "tenant_id", None), [copy], "",
                    remove_from_projects=True, reason="handed_over") != 1:
        session.rollback()  # nothing was deleted, so nothing committed
        return None
    logger.info("Asset %s moved to its copy at %s (copy %s removed)", asset_id, path, copy_id)
    tenant_id = getattr(request.state, "tenant_id", None)
    if archived.transcript_srt and tenant_id:
        try:
            from src.server.search.sync import index_transcript_segments

            index_transcript_segments(tenant_id, archived)
        except Exception as exc:  # noqa: BLE001 — the next search sync re-indexes it
            logger.warning("Couldn't re-index %s's transcript after the move: %s", asset_id, exc)
    return library_id


def purge_assets(
    session: Session,
    tenant_id: str | None,
    to_delete: list,
    user_id: str,
    *,
    remove_from_projects: bool,
    reason: str = "user",
    before: datetime | None = None,
) -> int:
    """Delete these trashed assets for good: rows, files, playback cuts and search
    documents. 409 in_projects when any are in projects, unless remove_from_projects.

    Re-checked under a row lock: only those still deleted for `reason` (since
    before `before`) go. One restored, archived or trashed again since it was
    listed keeps its files and everything else."""
    asset_repo = AssetRepository(session)
    still = set(asset_repo.lock_still_deleted([a.asset_id for a in to_delete], reason=reason, before=before))
    to_delete = [a for a in to_delete if a.asset_id in still]
    if not to_delete:
        return 0
    asset_ids = [a.asset_id for a in to_delete]
    if not remove_from_projects:
        from src.server.api.routers.assets import project_usage_summary

        usage = project_usage_summary(session, user_id, asset_ids=asset_ids)
        if usage.assets_in_projects:
            n = usage.assets_in_projects
            raise DecisionRequiredError(
                "in_projects",
                f"{n} of these clips {'is' if n == 1 else 'are'} in projects; deleting them for good "
                "removes them from those projects. Send remove_from_projects: true to go ahead.",
                usage.model_dump(),
            )
    # Collect keys for file cleanup before DB delete
    keys_to_remove: list[str] = []
    for a in to_delete:
        if a.proxy_key:
            keys_to_remove.append(a.proxy_key)
        if a.thumbnail_key:
            keys_to_remove.append(a.thumbnail_key)
        if getattr(a, "video_preview_key", None):
            keys_to_remove.append(a.video_preview_key)
        if getattr(a, "analysis_proxy_key", None):
            keys_to_remove.append(a.analysis_proxy_key)
    library_by_asset = {a.asset_id: a.library_id for a in to_delete}
    deleted_count = asset_repo.permanently_delete(asset_ids)
    storage = get_storage()
    for key in keys_to_remove:
        try:
            path = storage.abs_path(key)
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("Failed to remove file %s after empty trash: %s", key, e)
    if tenant_id:
        # Playback cuts are copies of the start of each video.
        from src.server.api.routers.playback import clear_cuts

        try:
            clear_cuts(tenant_id, asset_ids)
        except OSError as e:
            logger.warning("Failed to remove playback cuts after empty trash: %s", e)
        try:
            from src.server.search.quickwit_client import QuickwitClient

            QuickwitClient().delete_tenant_documents_by_asset_ids(tenant_id, asset_ids)
        except Exception as e:
            logger.warning("Quickwit delete after empty trash failed: %s", e)
    return deleted_count


def purge_expired_trash(session: Session, tenant_id: str, *, limit: int = 500) -> dict[str, int]:
    """Delete for good what has been in the trash longer than the account's
    trash days: clips a person trashed, libraries, projects. Nothing when an
    admin turned that off. Trashing is the consent (trashing what projects
    use asks first), so clips leave their projects without asking again.
    At most `limit` clips a run, so a run stays short; the next run takes more.
    """
    from datetime import timedelta

    from src.server.api.routers.libraries import delete_libraries_for_good
    from src.server.api.routers.projects import delete_projects_for_good
    from src.server.repository.tenant import ProjectRepository
    from src.server.tenant_settings import get_trash_days

    days = get_trash_days(session)
    if days is None:
        return {"clips": 0, "libraries": 0, "projects": 0}
    cutoff = utcnow() - timedelta(days=days)
    clips = AssetRepository(session).list_trashed(trashed_before=cutoff, limit=limit)
    n_clips = purge_assets(session, tenant_id, clips, "", remove_from_projects=True, before=cutoff) if clips else 0
    expired_libraries = [lib for lib in LibraryRepository(session).get_trashed()
                         if lib.trashed_at is not None and lib.trashed_at < cutoff]
    n_libraries = delete_libraries_for_good(session, tenant_id, expired_libraries, before=cutoff)
    n_projects = delete_projects_for_good(session, ProjectRepository(session).list_trashed_before(cutoff), before=cutoff)
    return {"clips": n_clips, "libraries": n_libraries, "projects": n_projects}

