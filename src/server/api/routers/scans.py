"""Scans: the client says what it found; the server decides (ADR-018 decision 12, phase 2).

A scan is a short-lived session. The client starts it before it walks, sends
every media file the walk saw, says the listing is complete and gets the plan,
sends the hashes the plan asks for and gets a decision for each, answers any
moves it was asked about, and finishes: what's gone is archived as missing,
with the same guard as DELETE /v1/assets. Before it finishes it can list
what it skips because a person removed it, and what's gone (GET /skipped,
/gone: `lumiverb scan --explain`). The rules are in src/server/scans.py.

The client still reads and uploads (POST /v1/ingest, as today), after the
finish: today's scanner archives before it ingests, so a file whose content
was just archived as missing comes back at its new path. Before moves are
answered or anything is finished, it checks its drive is still there (an
unmounted share is an empty folder): that stays the client's job.

Every call needs an editor (403 for a viewer). The account is the token's:
another account's scan is a 404. A scan expires a day after its last call
(410 scan_expired once it has); a finished one can't be used again (409),
but for its finish sent again.

Every call but the start is safe to send twice (the client tries again
when an answer was lost, src/processing/api.py): a repeat gets the answer
the first got. The finish is one transaction: the clips archived, those
handed over to their copies and the scan closed, all of it or none of it
when the API dies halfway (its files and search documents go after).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server import scans
from src.server.after_commit import AfterCommit
from src.server.api.dependencies import checked_rel_path, get_tenant_session, require_editor
from src.server.api.errors import ApiError, ConflictError, DecisionRequiredError
from src.server.api.limits import MAX_IDS, MAX_PAGE
from src.server.api.routers.assets import _ask_about_mass_missing
from src.server.api.routers.trash import hand_over_to_copies
from src.server.models.tenant import ScanSession
from src.server.repository import lineage
from src.server.repository.tenant import AssetRepository, LibraryRepository
from src.server.tenant_settings import get_follow_moves
from src.shared.io_utils import UnsafeRelPathError, check_rel_path
from src.shared.utils import utcnow

router = APIRouter(tags=["scans"], dependencies=[Depends(require_editor)])

# How many moves a 409 moved_files names.
MOVES_SHOWN = 10
# A path's longest (Linux's PATH_MAX): what a client sends is typed and bounded (ADR-018 decision 10).
MAX_PATH = 4096


class StartScanRequest(BaseModel):
    # A folder of the library ("" is the whole library): only clips under it can be gone.
    scope: str = Field(default="", max_length=MAX_PATH)
    media_type: Literal["all", "image", "video"] = "all"
    # Hash every file at a known path, not only those whose size or time changed.
    thorough: bool = False
    # Upload every file at a known path again; no moves are looked for.
    force: bool = False
    # A file's content at a new path, gone from its old one: moved now ("apply"),
    # never looked for ("skip": then nothing is archived either, since anything
    # gone may be half a move), or asked about ("ask": POST /moves).
    moves: Literal["apply", "skip", "ask"] = "ask"
    # Decide everything, change nothing.
    dry_run: bool = False
    # Add the files a person removed as if they never had (a last resort,
    # `lumiverb scan --force-add`): a clip in the trash or archived comes back
    # into sight when its content is at its path; a file deleted for good is a
    # new clip, and forgotten as deleted.
    force_add: bool = False


class StartScanResponse(BaseModel):
    scan_id: str
    # The account's "follow moves and renames": off, a file at a new path is new.
    follow_moves: bool
    expires_at: datetime


class FoundFile(BaseModel):
    rel_path: str = Field(max_length=MAX_PATH)
    file_size: int = Field(ge=0, le=2**63 - 1)  # 0: on disk, never uploaded (being written, or truncated)
    file_mtime: datetime | None = None
    # Written moments ago (may still be copying): present, so never gone, but nothing is planned for it.
    settling: bool = False


class FoundRequest(BaseModel):
    files: list[FoundFile] = Field(max_length=scans.MAX_FILES)


class FoundResponse(BaseModel):
    received: int


class PlanRequest(BaseModel):
    # Folders the walk couldn't list ("" is the library's own): nothing under
    # them is gone, and while there are any nothing is archived.
    unlisted: list[Annotated[str, Field(max_length=MAX_PATH)]] = Field(default_factory=list, max_length=MAX_IDS)


class PlannedFile(BaseModel):
    rel_path: str
    action: Literal["upload", "hash", "unchanged", "skipped"]
    # upload: new | preview_missing | force; hash: changed | thorough | removed | move;
    # skipped: removed; unchanged: none.
    reason: str | None = None
    # The clip at this path, when there is one.
    asset_id: str | None = None


class PlanCounts(BaseModel):
    found: int
    upload: int
    hash: int
    unchanged: int
    skipped: int
    settling: int
    empty: int
    # Clips of the scan's folder and media type it didn't find.
    gone: int


class PlanPage(BaseModel):
    files: list[PlannedFile]
    next_cursor: str | None = None


class PlanResponse(PlanPage):
    counts: PlanCounts


class SkippedFile(BaseModel):
    rel_path: str
    # How a person removed it: in the trash, archived, or deleted for good.
    removed_as: Literal["trash", "archived", "deleted"]
    # The clip removed (trash, archived); none for a file deleted for good.
    asset_id: str | None = None


class SkippedPage(BaseModel):
    files: list[SkippedFile]
    next_cursor: str | None = None


class GoneClip(BaseModel):
    asset_id: str
    rel_path: str
    media_type: str


class GonePage(BaseModel):
    clips: list[GoneClip]
    next_cursor: str | None = None


class HashedFile(BaseModel):
    rel_path: str = Field(max_length=MAX_PATH)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HashesRequest(BaseModel):
    files: list[HashedFile] = Field(max_length=scans.MAX_FILES)


class Decision(BaseModel):
    rel_path: str
    decision: Literal["unchanged", "upload", "moved", "move_candidate", "move_skipped", "skipped", "restored"]
    # The clip at this path (unchanged, upload), the clip that moved or would
    # move here, or the clip brought back into sight (restored: force_add).
    asset_id: str | None = None
    moved_from: str | None = None


class HashesResponse(BaseModel):
    files: list[Decision]


class MovesRequest(BaseModel):
    apply: bool


class MovesResponse(BaseModel):
    moved: int
    skipped: int
    # Moves that couldn't be made (the clip moved, changed or went meanwhile): upload these.
    upload: list[str]


class FinishRequest(BaseModel):
    # Archive even when it's most of a library (409 mass_missing otherwise).
    allow_mass_missing: bool = False


class FinishResponse(BaseModel):
    # Clips archived as missing (a dry run: that would be).
    archived: int
    # Moves made (a dry run: found), and clips handed over to an empty newer copy of their file.
    moved: int
    handed_over: int
    unchanged: int
    skipped: int
    # Clips gone but not archived, and why: a folder couldn't be listed, moves
    # were skipped, or a whole library's scan found nothing (more likely an
    # unmounted volume than an empty library).
    held_back: int
    held_back_reason: Literal["unlisted", "skip_moves", "nothing_found"] | None = None
    dry_run: bool


def _finished() -> ConflictError:
    return ConflictError("scan_finished", "This scan is finished: start another.")


def _held(session: Session, scan_id: str, *, planned: bool | None, finished: bool = False) -> ScanSession:
    """The scan, locked, its expiry pushed back a day; 404 unknown, 410
    expired, 409 finished (unless finished: the finish's own, sent again),
    or not at the step asked for (planned: True, after the plan; False,
    before it; None, either)."""
    scan = scans.lock(session, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    now = utcnow()
    if scan.expires_at <= now:
        scans.drop(session, scan_id)
        session.commit()
        raise ApiError(410, "scan_expired", "This scan expired: start another.")
    if scan.finished_at is not None and not finished:
        raise _finished()
    if planned and scan.planned_at is None:
        raise ConflictError("scan_not_planned", "Its listing isn't complete: send POST /v1/scans/{id}/plan first.")
    if planned is False and scan.planned_at is not None:
        raise ConflictError("scan_planned", "Its listing is complete: nothing more can be added.")
    scan.expires_at = now + scans.TTL
    session.add(scan)
    return scan


def _rel_paths(raw: list[str]) -> list[str]:
    """Each as stored (NFC); 400 for one that would leave the library."""
    return [checked_rel_path(p) for p in raw]


def _utc(when: datetime | None) -> datetime | None:
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


@router.post("/v1/libraries/{library_id}/scans", response_model=StartScanResponse)
def start_scan(
    library_id: str,
    body: StartScanRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> StartScanResponse:
    """Start a scan of a library, or of a folder in it, before walking it:
    a clip made or changed after this is never taken for gone by it. 409
    scans_paused while the library's scans are paused (its own pause, or
    Pause all): whoever reads it, nothing new is found in it then."""
    library = LibraryRepository(session).get_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if library.status == "trashed":
        raise ConflictError("library_trashed", "The library is in the trash.")
    if lineage.pauses(session).scans_paused(library_id):
        raise ConflictError("scans_paused", f"Scans of {library.name} are paused: resume them to scan it.",
                            {"library_id": library_id})
    try:
        scope = check_rel_path(body.scope.strip("/"), allow_root=True)
    except UnsafeRelPathError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {exc}") from None
    scan = scans.start(session, library_id, scope=scope, media_type=body.media_type, thorough=body.thorough,
                       force=body.force, moves=body.moves, dry_run=body.dry_run, force_add=body.force_add)
    return StartScanResponse(scan_id=scan.scan_id, follow_moves=get_follow_moves(session), expires_at=scan.expires_at)


@router.post("/v1/scans/{scan_id}/found", response_model=FoundResponse)
def add_found(
    scan_id: str,
    body: FoundRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FoundResponse:
    """Files the walk found, up to 1,000 a call: every media file in the
    scan's folder, empty and settling ones too. A path sent again takes its
    latest size and time."""
    scan = _held(session, scan_id, planned=False)
    found: list[scans.Found] = []
    for f, rel_path in zip(body.files, _rel_paths([f.rel_path for f in body.files]), strict=True):
        media_type = scans.media_type_of(rel_path)
        if media_type is None:
            raise HTTPException(status_code=400, detail=f"Not a media file: {rel_path}")
        if not scans.in_scope(rel_path, scan.scope):
            raise HTTPException(status_code=400, detail=f"Outside the scan's folder: {rel_path}")
        found.append(scans.Found(rel_path, f.file_size, _utc(f.file_mtime), media_type, f.settling))
    scans.add_found(session, scan.scan_id, found)
    session.commit()
    return FoundResponse(received=len(found))


def _actions(action: str | None) -> tuple[str, ...]:
    if action is None:
        return scans.TO_ACT_ON
    wanted = tuple(a.strip() for a in action.split(",") if a.strip())
    if not wanted or any(a not in scans.ACTIONS for a in wanted):
        raise ApiError(422, "invalid_request", f"action: one or more of {', '.join(scans.ACTIONS)}")
    return wanted


def _page(session: Session, scan_id: str, actions: tuple[str, ...], after: str | None, limit: int) -> PlanPage:
    rows = scans.page(session, scan_id, actions, after, limit)
    return PlanPage(files=[PlannedFile(**r) for r in rows],
                    next_cursor=rows[-1]["rel_path"] if len(rows) == limit else None)


@router.post("/v1/scans/{scan_id}/plan", response_model=PlanResponse)
def plan_scan(
    scan_id: str,
    body: PlanRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=MAX_PAGE, ge=1, le=MAX_PAGE),
    action: str | None = None,
) -> PlanResponse:
    """The listing is complete: plan every file, and return the counts and
    the plan's first page (GET for the rest). Sent again for the same
    unlisted folders (its answer lost), the same plan; for others, 409."""
    actions = _actions(action)
    scan = _held(session, scan_id, planned=None)
    try:
        unlisted = sorted({check_rel_path(u, allow_root=True) for u in body.unlisted})
    except UnsafeRelPathError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid unlisted folder: {exc}") from None
    if scan.planned_at is not None:
        counts = scans.answered(scan, "plan")
        if counts is None or list(scan.unlisted or []) != unlisted:
            raise ConflictError("scan_planned", "Its listing is complete: nothing more can be added.")
    else:
        scans.plan(session, scan, unlisted, follow_moves=get_follow_moves(session))
        counts = scans.counts(session, scan)
        scans.keep_answer(session, scan, "plan", counts)
    first = _page(session, scan.scan_id, actions, None, limit)
    session.commit()
    return PlanResponse(counts=PlanCounts(**counts), files=first.files, next_cursor=first.next_cursor)


@router.get("/v1/scans/{scan_id}/plan", response_model=PlanPage)
def plan_page(
    scan_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=MAX_PAGE, ge=1, le=MAX_PAGE),
    action: str | None = None,
) -> PlanPage:
    """A page of the plan, by rel_path. action: those to list, comma-separated
    (default upload,hash,skipped; unchanged lists the unchanged files with
    their clips)."""
    actions = _actions(action)
    scan = _held(session, scan_id, planned=True)
    result = _page(session, scan.scan_id, actions, after, limit)
    session.commit()
    return result


@router.get("/v1/scans/{scan_id}/skipped", response_model=SkippedPage)
def skipped_page(
    scan_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=MAX_PAGE, ge=1, le=MAX_PAGE),
) -> SkippedPage:
    """A page of the files the scan skips because a person removed them, by
    rel_path, each with how and the clip removed: what `lumiverb scan
    --explain` lists. After the plan (its hashes' decisions too, once sent)
    and before the finish, which drops the scan's listing; a dry run's too.
    Changes nothing."""
    scan = _held(session, scan_id, planned=True)
    rows = scans.skipped_page(session, scan, after, limit)
    session.commit()  # its expiry, pushed back
    return SkippedPage(files=[SkippedFile(**r) for r in rows],
                       next_cursor=rows[-1]["rel_path"] if len(rows) == limit else None)


@router.get("/v1/scans/{scan_id}/gone", response_model=GonePage)
def gone_page(
    scan_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=MAX_PAGE, ge=1, le=MAX_PAGE),
) -> GonePage:
    """A page of the clips the scan didn't find, by rel_path: what its finish
    archives as missing unless that's held back. Moves decided by then aren't
    gone. As GET /skipped: after the plan, before the finish; changes nothing."""
    scan = _held(session, scan_id, planned=True)
    rows = scans.gone_page(session, scan, after, limit)
    session.commit()  # its expiry, pushed back
    return GonePage(clips=[GoneClip(**r) for r in rows],
                    next_cursor=rows[-1]["rel_path"] if len(rows) == limit else None)


def _bump(session: Session, library_id: str, changed: bool) -> None:
    """Open grids and folder trees reload when clips moved."""
    if changed:
        LibraryRepository(session).bump_revision(library_id)


@router.post("/v1/scans/{scan_id}/hashes", response_model=HashesResponse)
def add_hashes(
    scan_id: str,
    body: HashesRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> HashesResponse:
    """Files' contents, up to 1,000 a call: a decision for each, in the order
    sent. Moves are made now when the scan applies them, and clips restored
    (force_add) are back in sight."""
    scan = _held(session, scan_id, planned=True)
    posted = dict(zip(_rel_paths([f.rel_path for f in body.files]), (f.sha256 for f in body.files), strict=True))
    try:
        decided = scans.decide(session, scan, list(posted.items()), follow_moves=get_follow_moves(session))
    except scans.NotPlannedError as exc:
        raise ApiError(422, "not_planned", "The plan doesn't list these files: send only files it lists.",
                       {"rel_paths": exc.rel_paths[:20], "count": len(exc.rel_paths)}) from None
    except scans.PathTakenError:
        session.rollback()
        raise ConflictError("path_taken", "Another clip was just put at one of these paths: send them again.") from None
    session.commit()
    _bump(session, scan.library_id, not scan.dry_run and any(d.decision in ("moved", "restored") for d in decided))
    return HashesResponse(files=[Decision(rel_path=d.rel_path, decision=d.decision, asset_id=d.asset_id,
                                          moved_from=d.moved_from) for d in decided])


@router.post("/v1/scans/{scan_id}/moves", response_model=MovesResponse)
def answer_moves(
    scan_id: str,
    body: MovesRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> MovesResponse:
    """A person's say on the moves the scan asked about: apply them, or skip
    them (the moved files take no part: not uploaded, their clips not archived).
    The same say sent again with none waiting (its answer lost) gets the
    answer the first got."""
    scan = _held(session, scan_id, planned=True)
    earlier = scans.answered(scan, "moves")
    if earlier is not None and earlier["apply"] == body.apply and not scans.move_candidates(session, scan.scan_id, 1):
        session.commit()  # its expiry, pushed back
        return MovesResponse(moved=earlier["moved"], skipped=earlier["skipped"], upload=earlier["upload"])
    if body.apply and not scan.dry_run and not get_follow_moves(session):
        raise DecisionRequiredError(
            "moves_off",
            "This account doesn't follow moves and renames: a file at a new path is a new asset. "
            "Turn follow_moves on (PATCH /v1/tenant/settings) to move assets, or skip them.",
            {"follow_moves": False},
        )
    try:
        moved, skipped, upload = scans.settle_moves(session, scan, body.apply)
    except scans.PathTakenError:
        session.rollback()
        raise ConflictError("path_taken", "Another clip was just put at one of these paths: answer again.") from None
    scans.keep_answer(session, scan, "moves", {"apply": body.apply, "moved": moved, "skipped": skipped,
                                               "upload": upload})
    session.commit()
    _bump(session, scan.library_id, bool(moved) and not scan.dry_run)
    return MovesResponse(moved=moved, skipped=skipped, upload=upload)


@router.post("/v1/scans/{scan_id}/finish", response_model=FinishResponse)
def finish_scan(
    scan_id: str,
    body: FinishRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> FinishResponse:
    """Archive what's gone as missing, as DELETE /v1/assets does (its guard
    against taking most of a library, and the handover to an empty newer copy
    of a file), and close the scan, in one transaction. A dry run archives
    nothing. Sent again (its answer lost), the answer the first one got."""
    scan = _held(session, scan_id, planned=True, finished=True)
    if scan.finished_at is not None:
        kept = scans.answered(scan, "finish")
        if kept is None:  # finished before answers were kept
            raise _finished()
        session.commit()  # its expiry, pushed back
        return FinishResponse(**kept)
    if not scan.dry_run and (waiting := scans.move_candidates(session, scan.scan_id)):
        shown = waiting[:MOVES_SHOWN]
        raise DecisionRequiredError(
            "moved_files",
            f"{len(waiting)} file(s) moved: apply or skip them first (POST /v1/scans/{scan_id}/moves).",
            {"count": len(waiting), "moves": [{"from": w["moved_from"], "to": w["rel_path"]} for w in shown]},
        )
    follow_moves = get_follow_moves(session)
    totals = scans.totals(session, scan)
    gone = scans.gone_ids(session, scan)
    reason = None
    if gone and scan.unlisted:
        reason = "unlisted"
    elif gone and scan.moves == "skip" and follow_moves:
        reason = "skip_moves"
    elif gone and not totals["of_media_type"] and not scan.force and not scan.scope:
        reason = "nothing_found"
    archiving = [] if reason else gone
    if archiving and not scan.dry_run and not body.allow_mass_missing:
        _ask_about_mass_missing(session, archiving, None, answer="allow_mass_missing: true")
    scans.close(session, scan)
    archived, handed_over = len(archiving), []
    after = AfterCommit()
    if archiving and not scan.dry_run:
        trashed, _ = AssetRepository(session).trash_many(archiving, reason="missing", commit=False)
        if trashed and follow_moves:
            handed_over = hand_over_to_copies(session, request, scans.with_newer_copies(session, trashed),
                                              after=after)
        archived = len(trashed) - len(handed_over)
    answer = FinishResponse(
        archived=archived, moved=totals["moved"] + len(handed_over), handed_over=len(handed_over),
        unchanged=totals["unchanged"], skipped=totals["skipped"],
        held_back=len(gone) if reason else 0, held_back_reason=reason, dry_run=scan.dry_run,
    )
    scans.keep_answer(session, scan, "finish", answer.model_dump())
    # The archive, the handovers, the scan's close and its answer, together:
    # an API that dies before this leaves the scan to be finished again,
    # never a clip archived whose copy it should have taken.
    session.commit()
    after.run()
    return answer


@router.delete("/v1/scans/{scan_id}", status_code=204)
def abandon_scan(
    scan_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Drop a scan and its listing, finished or not: nothing else changes
    (an aborted scan, or a mass_missing the person said no to)."""
    if not scans.drop(session, scan_id):
        raise HTTPException(status_code=404, detail="Scan not found")
    session.commit()
