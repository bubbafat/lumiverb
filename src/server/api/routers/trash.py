"""Trash API: empty trash (permanent delete). Admin only."""

import logging
from datetime import datetime
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session

from src.server.after_commit import AfterCommit
from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_signed_in, require_tenant_admin
from src.server.api.routers.archive import HiddenClip, decode_cursor, encode_cursor
from src.server.api.errors import DecisionRequiredError
from src.server.api.limits import MAX_IDS, MAX_PAGE, require_scope
from src.shared.utils import utcnow
from src.server.models.tenant import Asset
from src.server.repository.assets import AssetRepository
from src.server.repository.libraries import LibraryRepository
from src.server.storage import store as stored
from src.server.storage.store import Unavailable, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/trash", tags=["trash"])


class EmptyTrashRequest(BaseModel):
    # What goes: these clips, or all: true for every clip in the trash
    # (narrowed by library_id, path and trashed_before). Neither is a 400.
    asset_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)
    all: bool = False
    trashed_before: datetime | None = None
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
    # When this was listed (server time): send it back as trashed_before when
    # emptying, so nothing trashed since (never shown) goes with it.
    listed_at: str
    # The account's trash days; None when the trash is emptied only by hand.
    trash_days: int | None


@router.get("", response_model=TrashPage, dependencies=[Depends(require_signed_in)])
def list_trash(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    path: str | None = Query(default=None, description="Only clips under this folder (recursive)."),
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE),
) -> TrashPage:
    """Clips a person trashed, most recently trashed first, with when each is
    deleted for good. Clips of a library in the trash go with the library,
    so they're not here."""
    from datetime import timedelta

    from src.server.tenant_settings import get_trash_days

    days = get_trash_days(session)
    listed_at = utcnow()
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
        listed_at=listed_at.isoformat(),
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
    asset_ids, or all: true (every clip in the trash, narrowed by library_id,
    path and trashed_before); neither or both is a 400 scope_required.
    Deletes DB rows in FK-safe order (their search rows go with them), then best-effort file cleanup.
    """
    require_scope(body.asset_ids is not None, body.all, "asset_ids")
    asset_repo = AssetRepository(session)
    trashed_before_dt = body.trashed_before
    if body.all and trashed_before_dt is None:
        trashed_before_dt = utcnow()
    to_delete = asset_repo.list_trashed(asset_ids=body.asset_ids, trashed_before=trashed_before_dt,
                                        library_id=body.library_id, folder=body.path)
    if not to_delete:
        return EmptyTrashResponse(deleted=0)
    return EmptyTrashResponse(
        deleted=purge_assets(session, getattr(request.state, "tenant_id", None), to_delete, user_id,
                             remove_from_projects=body.remove_from_projects),
    )


def hand_over_to_copies(session: Session, request: Request, archived_ids: list[str], *,
                        after: AfterCommit | None = None) -> list[str]:
    """Copy, then delete the original (when the account follows moves): each of
    these just-archived assets whose content has an empty, newer copy in the
    library moves to that copy's path, with its notes, ratings, projects and
    people, and the copy goes. Returns the ids that moved.

    after given: in the caller's transaction, which the caller commits with
    the archive (a scan's finish, DELETE /v1/assets: both, or neither, when
    the API dies between them), then runs after: the copies' files, playback
    cuts and search documents go then, and open grids reload. Without it
    (ingest's, whose archive committed already): committed here, and those
    go at once.

    Each handover is its own savepoint: one that fails is logged and rolled
    back, the asset staying archived, never a failed request (the scan would
    stop, and the original, no longer listed, would never be tried again)."""
    if not archived_ids:
        return []
    own = after is None
    after = AfterCommit() if after is None else after
    # A handover gives way to a person within 200 ms (_hand_over); the rest
    # of the transaction waits as long as it ever did.
    lock_timeout = session.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    moved: list[str] = []
    libraries: set[str] = set()
    for asset_id in archived_ids:
        steps = AfterCommit()
        savepoint = session.begin_nested()
        try:
            library_id = _hand_over(session, request, asset_id, steps)
        except Exception as exc:  # noqa: BLE001 — the asset stays archived, as without moves
            savepoint.rollback()
            if _lock_timeout(exc):
                logger.info("Asset %s stays archived: its copy was busy (a person working on it)", asset_id)
            else:
                logger.exception("Couldn't move asset %s to its copy; it stays archived", asset_id)
            continue
        if library_id is None:
            savepoint.rollback()
            continue
        savepoint.commit()
        after.extend(steps)
        moved.append(asset_id)
        libraries.add(library_id)
    session.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": lock_timeout})
    for library_id in libraries:  # open grids drop the copies' tiles
        after.add(f"Bumping library {library_id}'s revision after a handover",
                  partial(LibraryRepository(session).bump_revision, library_id))
    if own:
        session.commit()
        after.run()
    return moved


def _lock_timeout(exc: Exception) -> bool:
    return getattr(getattr(exc, "orig", None), "pgcode", None) == "55P03"  # lock_not_available


def _hand_over(session: Session, request: Request, asset_id: str, after: AfterCommit) -> str | None:
    """Move one archived asset to its empty copy, in the caller's savepoint
    (no commit); what follows outside the database goes into after. Returns
    its library when it moved; None when there's nothing to move to (the
    caller rolls its savepoint back)."""
    repo = AssetRepository(session)
    archived = session.get(Asset, asset_id)  # get_by_id leaves out deleted assets
    if (archived is None or not repo.lock_for_restore(archived, archived.rel_path)
            or archived.deleted_at is None or archived.deleted_reason != "missing"):
        return None
    # The people routes lock faces first, as this does (tenant._lock_faces);
    # anything else a person does on the copy that locks in another order:
    # give way within 200 ms, before Postgres would call it a deadlock and
    # fail their request.
    session.execute(text("SET LOCAL lock_timeout = '200ms'"))
    copy = repo.find_empty_newer_copy(archived)
    if copy is None:
        return None
    path, copy_id, library_id = copy.rel_path, copy.asset_id, archived.library_id
    # Out of the way first: one clip in sight holds a path.
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
    # Deletes the copy for good, committed with the move; its files go after.
    tenant_id = getattr(request.state, "tenant_id", None)
    if purge_assets(session, tenant_id, [copy], "", remove_from_projects=True, reason="handed_over",
                    after=after) != 1:
        return None  # nothing was deleted: the caller rolls the move back
    session.expunge(copy)  # its row is gone
    logger.info("Asset %s moved to its copy at %s (copy %s removed)", asset_id, path, copy_id)
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
    after: AfterCommit | None = None,
) -> int:
    """Delete these trashed assets for good: rows, files, playback cuts and search
    documents. 409 in_projects when any are in projects, unless remove_from_projects.

    Re-checked under a row lock: only those still deleted for `reason` (since
    before `before`) go. One restored, archived or trashed again since it was
    listed keeps its files and everything else.

    after given: the rows go in the caller's transaction (no commit), and the
    files, playback cuts and search documents once the caller has committed
    and runs after. Without it: committed here, and they go at once."""
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
    keys_to_remove = _files_of(session, tenant_id, to_delete)  # read before their rows go
    deleted_count = asset_repo.permanently_delete(asset_ids, commit=after is None)
    cleanup = AfterCommit() if after is None else after
    cleanup.add("Removing deleted clips' files", partial(_remove_files, keys_to_remove))
    if tenant_id:
        cleanup.add("Removing deleted clips' playback cuts", partial(_clear_cuts, tenant_id, asset_ids))
    if after is None:
        cleanup.run()
    return deleted_count


def _files_of(session: Session, tenant_id: str | None, clips: list) -> list[str]:
    """Every file of these clips the previews folder holds: their previews
    and analysis proxies, their scenes' pictures and frames (a frame's key
    is derived, store.scene_rep_key: never in a column), and their faces'
    crops."""
    keys = [k for a in clips for k in (a.proxy_key, a.thumbnail_key, getattr(a, "video_preview_key", None),
                                       getattr(a, "analysis_proxy_key", None)) if k]
    ids = [a.asset_id for a in clips]
    library_of = {a.asset_id: a.library_id for a in clips}
    for asset_id, rep_frame_ms, proxy_key, thumbnail_key in session.execute(text(
        "SELECT asset_id, rep_frame_ms, proxy_key, thumbnail_key FROM video_scenes WHERE asset_id = ANY(:ids)"
    ), {"ids": ids}).all():
        keys += [k for k in (proxy_key, thumbnail_key) if k]
        if tenant_id:
            keys.append(stored.scene_rep_key(tenant_id, library_of[asset_id], asset_id, rep_frame_ms))
    for asset_id, face_id, crop_key in session.execute(text(
        "SELECT asset_id, face_id, crop_key FROM faces WHERE asset_id = ANY(:ids)"), {"ids": ids}).all():
        keys += [k for k in (crop_key, tenant_id and stored.face_crop_key_of(tenant_id, library_of[asset_id], face_id))
                 if k]
    return list(dict.fromkeys(keys))


def _remove_files(keys: list[str]) -> None:
    store = get_store()
    for key in keys:
        try:
            store.delete(key)  # wherever it is (a move may be under way)
        except Unavailable as e:  # left for the daily sweep once it can be reached
            logger.warning("Couldn't remove %s after empty trash: %s", key, e)
        except (OSError, ValueError) as e:  # ValueError: a key outside the previews folder, never deleted
            logger.warning("Failed to remove file %s after empty trash: %s", key, e)


def _clear_cuts(tenant_id: str, asset_ids: list[str]) -> None:
    """Playback cuts are copies of the start of each video."""
    from src.server.api.routers.playback import clear_cuts

    try:
        clear_cuts(tenant_id, asset_ids)
    except OSError as e:
        logger.warning("Failed to remove playback cuts after empty trash: %s", e)


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
    from src.server.repository.projects import ProjectRepository
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

