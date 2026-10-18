"""Why each producer's due work waits, as GET /v1/producers lists it on the
producer's row (`waits`): one entry for each reason (src/shared/waits.py)
holding some of it back now, with how many clips and what Settings →
Processing needs to say it.

Robert, Oct 12: work held back with no reason shown is a bug (analysis
proxies stopped for hours and the page said nothing). Every gate the
scheduler has is here, and tests/test_no_silent_waits.py holds each one to
it: what's paused, what a clip is made from, its AI machines, its library's
storage, a redo stopped by an admin or a new version, the clips the
scheduler holds back after a try (a scan comes first, or the try couldn't
finish), failures waiting their turn or given up, and the scheduler itself
(not running, starting, or unable to list the work). Each library's
scans say their own (scan_waits): a library whose scans keep failing.

Read from the account's database (pauses, lineage) and the scheduler's
status (scheduler.status: whether it runs, each library's storage, the
clips it holds back, its AI machines). What only the scheduler knows isn't
said while it isn't running: that it isn't running is said instead.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.repository import lineage
from src.shared.producers import PRODUCERS
from src.shared.waits import (
    AI_MACHINES,
    DEVICE,
    GIVEN_UP,
    HELD,
    NEEDS,
    NEW_VERSION,
    PAUSED,
    REASONS,
    REDO_STOPPED,
    RETRY_LATER,
    SCAN,
    SCAN_FAILING,
    SCHEDULER,
    STORAGE,
)

_ORDER = {reason: n for n, reason in enumerate(REASONS)}


def is_paused(artifact: str, paused: lineage.Pauses, library_id: str | None = None) -> bool:
    """Whether what makes an artifact is paused: its switch, or for what
    scans make (proxies), the library's scans (ADR-018 phase 1); with no
    library, every library's."""
    if PRODUCERS[artifact].scheduled:
        return artifact in paused.work
    return paused.scans_paused(library_id)


def producer_waits(session: Session, artifact: str, want: dict[str, Any], *, counts: dict[str, dict[str, int]],
                   paused: lineage.Pauses, status: Any, ai: Any = None, library_id: str | None = None,
                   asset_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Why the producer's due work waits: [{reason, clips, ...}] in the order
    of src/shared/waits.py, each only when some clips wait for it (an AI
    job's wait is said whatever: its work would). counts: every producer's
    lineage.counts over the clips in sight; status: the scheduler's (with
    live); ai: the control plane's word on its AI job (ai_machines.JobWait).
    In one project (asset_ids), the clips the scheduler holds back aren't
    counted: it doesn't say which clips they are. Scans make the producers
    it doesn't run: pausing Scans says so on their rows, and they list none."""
    p = PRODUCERS[artifact]
    if not p.scheduled:
        return []
    c = counts[artifact]
    redoable = lineage.redoable(artifact)
    redo_runs = redoable and artifact not in paused.redo
    due = c["due_new"] + (c["due_redo"] if redo_runs else 0)
    live = bool(status.live)
    out: list[dict[str, Any]] = []

    def add(reason: str, clips: int, **details: Any) -> None:
        if clips > 0:
            out.append({"reason": reason, "clips": clips, **details})

    if artifact in paused.work:
        add(PAUSED, due)
    # The scheduler hands nothing out: not running, or not yet sure of the settings.
    unlisted = next((status.unlisted[k] for k in (p.kind, f"redo_{p.kind}") if k in status.unlisted), None)
    if not live:
        add(SCHEDULER, due, why="not_running")
    elif not status.settings_ready:
        add(SCHEDULER, due, why="starting")
    elif unlisted is not None:
        add(SCHEDULER, due, why="cant_list", message=unlisted)
    if p.job:
        # The control plane's word (machines and models as Settings → AI keeps
        # them), else the scheduler's own: it found none it can use.
        why, message = (ai.why, ai.message) if ai is not None else (
            ("unusable", status.ai_waits[p.job]) if live and p.job in status.ai_waits else (None, None))
        if why:
            out.append({"reason": AI_MACHINES, "clips": due, "job": p.job, "why": why, "message": message})
    for need in p.needs:
        add(NEEDS, counts[need]["missing"] if need in counts else 0, artifact=need,
            paused=is_paused(need, paused, library_id))
    # Pause all touches no drive, so nothing looks at them: paused says it.
    if p.storage and live and not paused.everything:
        away = [i for i in status.storage_waits if library_id is None or i == library_id]
        due_in = lineage.due_by_library(session, artifact, want, away, redo=redo_runs, asset_ids=asset_ids)
        names = _library_names(session, [i for i in away if due_in.get(i)])
        for i in away:
            seen = status.storage.get(i)
            add(STORAGE, due_in.get(i, 0), library_id=i, library_name=names.get(i, ""),
                seen_at=getattr(seen, "seen_at", None), away_since=getattr(seen, "away_since", None),
                checked=bool(getattr(seen, "checked", False)))
    if p.storage:
        # A device reads these libraries: what needs their originals is its (ADR-018 decision 1).
        devices = _read_by_devices(session, library_id)
        due_in = lineage.due_by_library(session, artifact, want, list(devices), redo=redo_runs, asset_ids=asset_ids)
        for i, lib in devices.items():
            add(DEVICE, due_in.get(i, 0), library_id=i, library_name=lib["name"], device_id=lib["reader"],
                device_name=lib["device_name"] or "", checked_in_at=lib["checked_in_at"],
                remakes_held=lib["remakes_held"])
    if redoable and artifact in paused.redo:
        # A new version stops its redo by no one (lineage.hold_new_version); an admin, by name.
        add(NEW_VERSION if paused.redo[artifact]["paused_by"] is None else REDO_STOPPED, c["due_redo"])
    if live and asset_ids is None:
        held: dict[str, int] = {}
        scan_paused = False  # a library a scan comes first in has its scans paused
        for row in status.held:
            if row.kind == p.kind and (library_id is None or row.library_id == library_id):
                why = row.why if row.why in (SCAN, HELD) else HELD  # a code it doesn't know: held
                held[why] = held.get(why, 0) + row.clips
                scan_paused |= why == SCAN and row.clips > 0 and paused.scans_paused(row.library_id or None)
        add(SCAN, held.get(SCAN, 0), paused=scan_paused)
        add(HELD, held.get(HELD, 0))
    add(RETRY_LATER, c["retrying"])
    add(GIVEN_UP, c["given_up"])
    return sorted(out, key=lambda w: _ORDER[w["reason"]])


def scan_waits(session: Session, status: Any) -> list[dict[str, Any]]:
    """Why libraries' scans wait, as each library's row says it (GET
    /v1/producers/queue): each library whose scans keep failing, with the
    error, since when and the next try (SCAN_FAILING), while the scheduler
    runs (only it knows; that it isn't running is said instead)."""
    if not status.live or not status.scans_failing:
        return []
    names = _library_names(session, [f.library_id for f in status.scans_failing])
    return [{"reason": SCAN_FAILING, "clips": 0, "library_id": f.library_id,
             "library_name": names.get(f.library_id, ""), "message": f.error, "since": f.since,
             "next_try": f.next_try} for f in status.scans_failing]


def _read_by_devices(session: Session, library_id: str | None) -> dict[str, dict[str, Any]]:
    """The active libraries a device reads (that one, when given), each
    with its name, its device's id and name, when that device last checked
    in for it, and why it holds its remakes back then (remakes_held)."""
    rows = session.execute(text(
        "SELECT l.library_id, l.name, l.reader, d.name AS device_name, c.checked_in_at, c.remakes_held"
        " FROM libraries l LEFT JOIN devices d ON d.device_id = l.reader"
        " LEFT JOIN reader_check_ins c ON c.library_id = l.library_id AND c.reader = l.reader"
        " WHERE l.status = 'active' AND l.reader <> 'server'"
        "   AND (CAST(:lib AS text) IS NULL OR l.library_id = :lib)"), {"lib": library_id}).mappings().all()
    return {r["library_id"]: dict(r) for r in rows}


def _library_names(session: Session, library_ids: list[str]) -> dict[str, str]:
    if not library_ids:
        return {}
    rows = session.execute(text("SELECT library_id, name FROM libraries WHERE library_id = ANY(:ids)"),
                           {"ids": list(library_ids)}).all()
    return {r[0]: r[1] for r in rows}
