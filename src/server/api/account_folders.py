"""Each account's folders on this server: where the libraries this server
reads for it may be, the server admin's to give (tenants.folders; PUT
/v1/admin/tenants/{id}/folders). With several accounts on one server, an
account admin could otherwise point a library at any folder the server
reads, another account's too.

None, every account's until a server admin gives it folders, is every
folder the server reads, as before: a server with one account sees nothing
of this. Given folders (a list, maybe empty), a library this server reads
is in one of them, its root equal to one or inside it, part by whole part
(io_utils.in_folder: /a/b isn't in /a/bc). It's checked wherever such a
library gets its folder (routers/libraries.py: made, moved, handed to this
server), 403 outside_account_folders otherwise, and by the scheduler as it
reaches the library, its folder resolved on disk (the check-in's `folders`,
src/processing/roots.py), so a link inside an account's folder leads
nowhere else. A library a device reads isn't affected: its folder is on the
account's own computer.

A library's folder is checked and saved with the account's folders held
(choosing: FOR SHARE until it's saved), and a change of them waits for it,
then finds the library (set_folders); one change of any account's folders
at a time, so two accounts are never given overlapping ones at once."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlmodel import Session

from src.server.api.errors import ApiError, ConflictError, InvalidChoiceError
from src.shared.io_utils import in_folder, root_in_form

logger = logging.getLogger(__name__)

# The most folders an account is given, and the longest one (a path on Linux or a Mac).
MAX_FOLDERS = 100
MAX_FOLDER_LENGTH = 4096


def in_them(root_path: str, folders: list[str] | None) -> bool:
    """A library's folder is in the account's folders (None: every folder the server reads)."""
    return folders is None or any(in_folder(root_path, f) for f in folders)


def outside(folders: list[str]) -> ApiError:
    """403 outside_account_folders, saying the account's folders."""
    if folders:
        said = f"This account's folders are {', '.join(folders)}: choose one inside them, or ask a server admin."
    else:
        said = ("This account has no folders on this server: a computer of its own can read its libraries "
                "(Settings › Devices), or ask a server admin.")
    return ApiError(403, "outside_account_folders", said, {"folders": folders})


def of(tenant_id: str | None) -> list[str] | None:
    """The account's folders, as they are now (None: every folder the server reads)."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    if not tenant_id:
        return None
    with get_control_session() as ctrl:
        return TenantRepository(ctrl).folders_of(tenant_id)


@contextlib.contextmanager
def choosing(request: Request, root_path: str | None) -> Iterator[None]:
    """For the block that saves a library's folder on this server
    (root_path; None when the server doesn't read the library, or its folder
    isn't changing): 403 outside_account_folders unless it's in the
    account's folders, which can't change until the block ends."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    tenant_id = getattr(request.state, "tenant_id", None)
    if root_path is None or not tenant_id:
        yield
        return
    with get_control_session() as ctrl:  # ends rolled back (nothing changed), and they can change again
        folders = TenantRepository(ctrl).folders_of(tenant_id, hold=True)
        if not in_them(root_path, folders):
            raise outside(folders or [])
        yield


def checked(folders: list[str]) -> list[str]:
    """Folders a server admin gives, each once, in the order given: 422
    invalid_folder for one not in the form library roots are kept in, and
    folder_not_read for one outside what the server reads, where its root
    map bounds that (LUMIVERB_ROOT_MAP naming folders: a Linux install's)."""
    from src.processing.roots import maps, reads_every_folder
    from src.server.scheduler.settings import SchedulerSettings

    root_map = SchedulerSettings().root_map
    bounded = bool(root_map) and not reads_every_folder(root_map)
    kept: list[str] = []
    for folder in folders:
        if not root_in_form(folder) or len(folder) > MAX_FOLDER_LENGTH:
            raise InvalidChoiceError(
                "invalid_folder",
                f"{folder!r} isn't a folder as Lumiverb keeps one: a full path such as /Volumes/Photos, with no "
                "., .. or empty part (no / at the end), no backslash and no control character.",
                {"folder": folder})
        if bounded and not maps(folder, root_map):
            raise InvalidChoiceError(
                "folder_not_read",
                f"This server doesn't read {folder}: its root map (LUMIVERB_ROOT_MAP) reads only "
                f"{', '.join(sorted(root_map))}.",
                {"folder": folder, "root_map": sorted(root_map)})
        if folder not in kept:
            kept.append(folder)
    return kept


def _left_outside(tenant_id: str, name: str, folders: list[str]) -> list[dict]:
    """The account's libraries this server reads (in the trash too: restored,
    they're read again) whose folders aren't in these. 503
    account_unavailable when its database can't be read."""
    from src.server.database import get_tenant_session
    from src.server.repository.libraries import LibraryRepository
    from src.shared.readers import read_by_server

    try:
        with get_tenant_session(tenant_id) as tenant_session:
            libraries = [lib for lib in LibraryRepository(tenant_session).list_all(include_trashed=True)
                         if read_by_server(lib)]
            return [{"library_id": lib.library_id, "name": lib.name, "root_path": lib.root_path,
                     "trashed": lib.status == "trashed"}
                    for lib in sorted(libraries, key=lambda lib: lib.name) if not in_them(lib.root_path, folders)]
    except Exception as exc:  # noqa: BLE001 — its database away: nothing is changed, and it's said so
        logger.warning("Account %s's database can't be read: %s", tenant_id, exc)
        raise ApiError(503, "account_unavailable",
                       f"{name}: its database can't be read now, so its libraries can't be checked against these "
                       "folders. Try again later.") from None


def set_folders(session: Session, tenant_id: str, folders: list[str] | None) -> list[str] | None:
    """Give the account these folders (None: every folder the server reads),
    on the control database's session, and return them as kept. 422 as
    checked says; 404 for no such account, or a deleted one; 409
    folders_overlap for one equal to, inside or holding another live
    account's (details: folder, account, tenant_id, theirs); 409
    libraries_outside when a library this server reads for it would be
    outside them (details.libraries: library_id, name, root_path, trashed);
    503 account_unavailable when its libraries can't be read."""
    from src.server.repository.control_plane import DELETED, TenantRepository

    kept = None if folders is None else checked(folders)
    repo = TenantRepository(session)
    tenant = repo.to_change_folders(tenant_id)
    if tenant is None or tenant.status == DELETED:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if kept is not None:
        for other in repo.list_live():
            if other.tenant_id == tenant_id:
                continue
            for theirs in other.folders or []:
                for mine in kept:
                    if in_folder(mine, theirs) or in_folder(theirs, mine):
                        raise ConflictError(
                            "folders_overlap",
                            f"{mine} overlaps {theirs}, a folder of {other.name}: each account's folders are its own.",
                            {"folder": mine, "account": other.name, "tenant_id": other.tenant_id, "theirs": theirs})
        left = _left_outside(tenant_id, tenant.name, kept)
        if left:
            one = len(left) == 1
            named = ", ".join(f"{lib['name']} ({lib['root_path']}{', in the trash' if lib['trashed'] else ''})"
                              for lib in left)
            raise ConflictError(
                "libraries_outside",
                f"{named} would be outside these folders: move {'it' if one else 'them'} first (Library settings › "
                f"Who reads it), or include {'its folder' if one else 'their folders'}.",
                {"libraries": left})
    repo.set_folders(tenant, kept)
    return kept
