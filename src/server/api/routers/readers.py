"""A library's reader: its check-in, what changed for it, and a scan asked of
it (ADR-018 decision 4, phase 4). The rules are in src/server/check_ins.py.

Every reader checks in every minute or so (POST /v1/readers/check-in); the
answer lists the libraries it reads with each one's mark, and when a mark
moved it fetches what changed (GET /v1/libraries/{id}/reader-changes). This
server's scheduler reads every library no device reads
(src/shared/readers.py), checking in with an admin's key, as the
scheduler's is; a device checks in with its own key
(src/server/api/devices.py). An editor can ask a library's reader for a
scan of the whole library (POST /v1/libraries/{id}/scan-now).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server import check_ins
from src.server.api.dependencies import get_tenant_session, require_editor, require_reader
from src.server.api.devices import may_call
from src.server.api.errors import ApiError, ConflictError
from src.server.api.limits import MAX_IDS
from src.server.repository.libraries import LibraryRepository
from src.shared.readers import SERVER
from src.shared.utils import utcnow

router = APIRouter(tags=["readers"])


class LibrarySaid(BaseModel):
    library_id: str = Field(max_length=200)
    # scanning; idle; paused (its scans); away (its storage can't be reached from the reader).
    state: Literal["scanning", "idle", "paused", "away"]
    waiting_files: int = Field(default=0, ge=0, le=2**62)
    waiting_bytes: int = Field(default=0, ge=0, le=2**62)
    last_full_scan_at: datetime | None = None
    # The mark it last fetched what changed at (0: never).
    mark: int = Field(default=0, ge=0, le=2**62)


class CheckInRequest(BaseModel):
    # Who reads: "server", this server's scheduler. A device needn't say: its key says which it is.
    reader: Literal["server"] = SERVER
    # What it is, for people: "this server", "Robert's Mac Studio"; and its version.
    client: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    # The conversation's version it speaks (check_ins.PROTOCOL).
    protocol: int = Field(ge=1)
    libraries: list[LibrarySaid] = Field(default_factory=list, max_length=MAX_IDS)


class LibraryToRead(BaseModel):
    library_id: str
    name: str
    root_path: str
    reader: str
    # Its scans are paused (its own pause, or Pause all): nothing new is found in it.
    scans_paused: bool
    # Moves when something the reader acts on changes: when it's not the one the reader has, fetch what changed.
    mark: int
    # A scan of the whole library was asked for.
    scan_now: bool


class CheckInResponse(BaseModel):
    libraries: list[LibraryToRead]
    # Libraries the reader named that it no longer reads (in the trash, deleted, another reader's).
    gone: list[str]
    check_in_every_sec: int


class LibraryFilters(BaseModel):
    includes: list[str]
    excludes: list[str]


class Filters(BaseModel):
    # The account's defaults and the library's own, merged as src/shared/path_filter.py is_path_included_merged says.
    account: LibraryFilters
    library: LibraryFilters


class MadeWith(BaseModel):
    producer: str
    version: str
    settings: dict
    settings_hash: str


class ReaderLibrary(BaseModel):
    library_id: str
    name: str
    root_path: str
    reader: str
    status: str
    scans_paused: bool
    scan_now: bool


class Paused(BaseModel):
    # What it makes from the originals that's paused (not to be made now), and whose remakes are stopped.
    work: list[str]
    remakes: list[str]


class ReaderChanges(BaseModel):
    mark: int
    # Everything, not only what changed since: true until the server keeps a history.
    everything: bool
    library: ReaderLibrary
    filters: Filters
    # What each thing the reader makes is made with now, by artifact (Producers API).
    made_with: dict[str, MadeWith]
    paused: Paused


@router.post("/v1/readers/check-in", response_model=CheckInResponse, dependencies=[Depends(require_reader)])
@may_call("own")
def check_in(body: CheckInRequest, request: Request,
             session: Annotated[Session, Depends(get_tenant_session)]) -> CheckInResponse:
    """A reader says how each library it reads is, and hears which it reads
    now: each one's root, scans pause, mark, and whether a scan of it was
    asked for. Every minute or so (check_in_every_sec). This server's
    reader with an admin's key, as the scheduler's is; a device with its
    own, which reads the libraries naming it (src/server/api/devices.py).
    426 reader_too_old for a protocol the server no longer speaks."""
    if body.protocol < check_ins.OLDEST_PROTOCOL:
        raise ApiError(426, "reader_too_old", "This reader is too old for this server: update it.",
                       {"oldest_protocol": check_ins.OLDEST_PROTOCOL, "protocol": check_ins.PROTOCOL})
    said = [check_ins.Said(s.library_id, s.state, s.waiting_files, s.waiting_bytes, _utc(s.last_full_scan_at), s.mark)
            for s in body.libraries]
    reader = getattr(request.state, "device_id", None) or body.reader
    libraries, gone = check_ins.check_in(session, reader=reader, client=body.client, version=body.version,
                                         said=said, now=utcnow())
    session.commit()
    return CheckInResponse(libraries=[LibraryToRead(**lib) for lib in libraries], gone=gone,
                           check_in_every_sec=check_ins.CHECK_IN_EVERY_SEC)


@router.get("/v1/libraries/{library_id}/reader-changes", response_model=ReaderChanges,
            dependencies=[Depends(require_editor)])
@may_call("library")
def reader_changes(
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    since: int = Query(default=0, ge=0, le=2**62),
) -> ReaderChanges:
    """What the library's reader acts on, as of its mark now: the library,
    its path filters and the account's, what each thing a reader makes is
    made with, and what of that is paused. For now everything, whatever
    since (everything: true). 404 for no such library."""
    found = check_ins.changes(session, request.state.tenant_id, library_id, since)
    if found is None:
        raise HTTPException(status_code=404, detail="Library not found")
    return ReaderChanges(**found)


@router.post("/v1/libraries/{library_id}/scan-now", status_code=204, dependencies=[Depends(require_editor)])
def scan_now(library_id: str, session: Annotated[Session, Depends(get_tenant_session)]) -> None:
    """Ask the library's reader for a scan of the whole library, at its next
    check-in. Asked again before it's done, the first ask stands; a scan of
    the whole library started after it answers it. 404 unknown, 409
    library_trashed, 409 scans_paused while its scans are paused."""
    from src.server.repository import lineage

    library = LibraryRepository(session).get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if library.status == "trashed":
        raise ConflictError("library_trashed", "The library is in the trash.")
    if lineage.pauses(session).scans_paused(library_id):
        raise ConflictError("scans_paused", f"Scans of {library.name} are paused: resume them to scan it.",
                            {"library_id": library_id})
    check_ins.request_scan(session, library_id, utcnow())
    session.commit()


def _utc(when: datetime | None) -> datetime | None:
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when
