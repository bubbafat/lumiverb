"""A library's reader checks in, and a mark per library tells it when to
fetch what changed on the server (ADR-018 decision 4, phase 4).

A reader (this server's scheduler; a device from phase 6) checks in every
minute or so, saying of each library it reads: scanning, idle, paused, or
away (its storage can't be reached); the files and bytes it has waiting to
upload; its last full scan; and the mark it last fetched at. The server
keeps each library's latest check-in (reader_check_ins) and answers with
the libraries the reader reads: each one's root, whether its scans are
paused, its mark, and whether a scan of the whole library was asked for
(Scan now). A library the reader named that it no longer reads (in the
trash, deleted, another reader's) comes back as gone. The answer is the
server's one way to reach a reader behind a home router.

The mark (libraries.reader_mark) moves whenever something the reader acts
on changes on the server: the library's root, status, reader or scans
pause, a scan asked for; its path filters, or the account's; any pause;
any producer's settings, or a new version of one seen; failing clips asked
to be tried again; a clip's preview, video preview or analysis proxy lost.
The database's triggers move it (migration a8d4f2c6e1b9), in the change's
own transaction, so no writer can forget it, and a reader that sees a new
mark sees the change. The new clips a reader's own scans make don't move
it: the reader knows. When the mark moves, the reader fetches what changed
(changes): for now everything, and the answer says so, as it can become
only what changed without any reader changing; mark 0 asks for everything
too, a fresh start. It's never a copy of the library (decision 13): which
files there are is the scan's to compare, and what needs the originals is
the list in decision 3 (src/server/reader_work.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.reader_work import FROM_ORIGINALS, MADE_AT_SCAN
from src.server.repository import lineage

STATES = ("scanning", "idle", "paused", "away")
# Why a reader holds its remakes back (a device's: they wait for mains power, or a network that isn't metered).
REMAKES_HELD = ("battery", "metered")
# How often a reader checks in (the answer says it).
CHECK_IN_EVERY_SEC = 60
# The conversation's version, and the oldest a reader may speak (426 reader_too_old below it).
PROTOCOL = 1
OLDEST_PROTOCOL = 1
# What a reader makes, whose settings and pauses it's told.
READER_MAKES = (*MADE_AT_SCAN, *FROM_ORIGINALS)


@dataclass(frozen=True)
class Said:
    """What a reader says of one library when it checks in."""

    library_id: str
    state: str
    waiting_files: int = 0
    waiting_bytes: int = 0
    last_full_scan_at: datetime | None = None
    mark: int = 0
    # Why it holds its remakes back now (ADR-018 decision 6): REMAKES_HELD, or None when they go on.
    remakes_held: str | None = None


def waiting(left: tuple[int, int] | None) -> dict[str, int]:
    """What a reader's check-in says it has waiting to upload in a library,
    from what its latest scan there has left to send (files, their size on
    disk; None: no scan said)."""
    files, size = left or (0, 0)
    return {"waiting_files": max(0, files), "waiting_bytes": max(0, size)}


def check_in(session: Session, *, reader: str, client: str, version: str, said: list[Said],
             now: datetime) -> tuple[list[dict], list[str]]:
    """Keep what the reader says of each library it reads (no commit), and
    return the libraries it reads, by name, each {library_id, name,
    root_path, reader, scans_paused, mark, scan_now}, and of the libraries
    it named, those it no longer reads (gone)."""
    paused = lineage.pauses(session)
    rows = session.execute(text(
        "SELECT library_id, name, root_path, reader, reader_mark, scan_requested_at FROM libraries"
        " WHERE status = 'active' AND reader = :reader ORDER BY name, library_id"
    ), {"reader": reader}).mappings().all()
    reads = {r["library_id"] for r in rows}
    gone = sorted({s.library_id for s in said if s.library_id not in reads})
    kept = {s.library_id: s for s in said if s.library_id in reads}
    if kept:
        session.execute(text("""
            INSERT INTO reader_check_ins (library_id, reader, client, version, state, waiting_files, waiting_bytes,
                                          last_full_scan_at, mark, remakes_held, checked_in_at)
            SELECT k.library_id, :reader, :client, :version, k.state, k.files, k.bytes, k.full_scan, k.mark, k.held,
                   :now
            FROM unnest(CAST(:ids AS text[]), CAST(:states AS text[]), CAST(:files AS bigint[]),
                        CAST(:bytes AS bigint[]), CAST(:full AS timestamptz[]), CAST(:marks AS bigint[]),
                        CAST(:held AS text[]))
                 AS k(library_id, state, files, bytes, full_scan, mark, held)
            ON CONFLICT (library_id) DO UPDATE SET
                reader = EXCLUDED.reader, client = EXCLUDED.client, version = EXCLUDED.version,
                state = EXCLUDED.state, waiting_files = EXCLUDED.waiting_files,
                waiting_bytes = EXCLUDED.waiting_bytes, last_full_scan_at = EXCLUDED.last_full_scan_at,
                mark = EXCLUDED.mark, remakes_held = EXCLUDED.remakes_held, checked_in_at = EXCLUDED.checked_in_at
        """), {"reader": reader, "client": client, "version": version, "now": now, "ids": list(kept),
               "states": [s.state for s in kept.values()], "files": [s.waiting_files for s in kept.values()],
               "bytes": [s.waiting_bytes for s in kept.values()],
               "full": [s.last_full_scan_at for s in kept.values()], "marks": [s.mark for s in kept.values()],
               "held": [s.remakes_held for s in kept.values()]})
    return [{"library_id": r["library_id"], "name": r["name"], "root_path": r["root_path"], "reader": r["reader"],
             "scans_paused": paused.scans_paused(r["library_id"]), "mark": r["reader_mark"],
             "scan_now": r["scan_requested_at"] is not None} for r in rows], gone


def latest(session: Session) -> dict[str, dict[str, Any]]:
    """Each library's latest check-in, by library: its reader and client,
    the client's version, state, files and bytes waiting, last full scan,
    the mark it had and when it checked in."""
    return {r["library_id"]: dict(r) for r in session.execute(text(
        "SELECT library_id, reader, client, version, state, waiting_files, waiting_bytes, last_full_scan_at,"
        " mark, remakes_held, checked_in_at FROM reader_check_ins")).mappings()}


def request_scan(session: Session, library_id: str, now: datetime) -> None:
    """Ask the library's reader for a scan of the whole library (no commit).
    Asked again before it's done, the first ask stands."""
    session.execute(text("UPDATE libraries SET scan_requested_at = :now"
                         " WHERE library_id = :l AND scan_requested_at IS NULL"), {"l": library_id, "now": now})


def scan_finished(session: Session, *, library_id: str, scope: str, media_type: str, dry_run: bool,
                  started_at: datetime) -> None:
    """A scan finished (no commit): one of the whole library, every media
    type, that changes things and started after the ask, answers it."""
    if scope or media_type != "all" or dry_run:
        return
    session.execute(text("UPDATE libraries SET scan_requested_at = NULL"
                         " WHERE library_id = :l AND scan_requested_at <= :started"),
                    {"l": library_id, "started": started_at})


def changes(session: Session, tenant_id: str, library_id: str, since: int) -> dict[str, Any] | None:
    """What the library's reader acts on, as of its mark now: the library,
    its path filters and the account's, what each thing a reader makes is
    made with, and what of it is paused. Everything for now, whatever
    since: the answer says so (everything). None for no such library."""
    from src.server.repository.libraries import PathFilterRepository

    lib = session.execute(text(
        "SELECT library_id, name, root_path, reader, status, reader_mark, scan_requested_at FROM libraries"
        " WHERE library_id = :l"), {"l": library_id}).mappings().first()
    if lib is None:
        return None
    paused = lineage.pauses(session)
    filters = PathFilterRepository(session)

    def patterns(rows: list) -> dict[str, list[str]]:
        return {"includes": sorted(f.pattern for f in rows if f.type == "include"),
                "excludes": sorted(f.pattern for f in rows if f.type == "exclude")}

    return {
        "mark": lib["reader_mark"], "everything": True,
        "library": {"library_id": lib["library_id"], "name": lib["name"], "root_path": lib["root_path"],
                    "reader": lib["reader"], "status": lib["status"],
                    "scans_paused": paused.scans_paused(library_id),
                    "scan_now": lib["scan_requested_at"] is not None},
        "filters": {"account": patterns(filters.list_defaults(tenant_id)),
                    "library": patterns(filters.list_for_library(library_id))},
        "made_with": {a: _made_with(session, a) for a in READER_MAKES},
        "paused": {"work": sorted(a for a in FROM_ORIGINALS if a in paused.work),
                   "remakes": sorted(a for a in FROM_ORIGINALS if a in paused.redo)},
    }


def _made_with(session: Session, artifact: str) -> dict[str, Any]:
    """What a current one is made with now: producer, version, settings and their hash."""
    want = lineage.desired(session, artifact)
    return {"producer": want["producer"], "version": want["version"], "settings": want["settings"],
            "settings_hash": want["settings_hash"]}
