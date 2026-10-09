"""System health for the Admin page: six rows, each green, yellow or red
with a one-line reason and where to fix it (GET /v1/system/health,
routers/system.py gathers the inputs).

The rules here take plain inputs so each state can be tested on its own.
The thresholds are Proposed (Robert, Oct 9): change them here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from src.shared.ai_jobs import JOBS

GREEN, YELLOW, RED = "green", "yellow", "red"
_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# The scheduler writes its status every few seconds; silent this long, it isn't running
# (the same 30 seconds as GET /v1/producers/queue's live).
SCHEDULER_SILENT = timedelta(seconds=30)
# Clips that failed this recently make Processing yellow; older failures are
# listed in Settings → Processing but don't light the dot. Proposed.
FAILING_WINDOW = timedelta(hours=24)
# The scheduler checks each machine of a job that's on every minute; not
# checked for this long, nothing is checking it. Proposed.
MACHINE_STALE = timedelta(minutes=10)
# A search that fell back to Postgres, or failed, this recently colours Search. Proposed.
SEARCH_RECENT = timedelta(minutes=10)
# This many clips not in the search index yet: search can miss them. Proposed.
UNSYNCED_YELLOW = 100
# Free space on the data disk, as a share of it. Proposed.
DISK_YELLOW = 0.15
DISK_RED = 0.05


@dataclass
class Row:
    key: str
    title: str
    state: str
    reason: str
    link: str | None = None
    checked_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def overall(rows: Iterable[Row]) -> str:
    """The worst of the rows' states."""
    return max((r.state for r in rows), key=_RANK.__getitem__, default=GREEN)


def ago(delta: timedelta) -> str:
    """'40 seconds', '1 minute', '2 hours', '3 days'."""
    seconds = max(0, int(delta.total_seconds()))
    for unit, size, least in (("day", 86400, 2), ("hour", 3600, 1), ("minute", 60, 1)):
        if seconds >= size * least:
            n = round(seconds / size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} second{'' if seconds == 1 else 's'}"


def _clips(n: int) -> str:
    return f"{n:,} clip{'' if n == 1 else 's'}"


def _names(names: Sequence[str]) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# -- Website --------------------------------------------------------------------


def website_row(*, db_error: str | None, now: datetime, maintenance: str | None = None) -> Row:
    """Green when the API answers and so does the database (a cheap query).
    Red when the database doesn't. Yellow in maintenance mode, which holds
    the workers back (Proposed). The API not answering at all is the web
    page's to say: this endpoint can't."""
    row = Row("website", "Website", GREEN, "The API and the database are answering.", checked_at=now)
    if db_error:
        row.state, row.reason = RED, f"The database isn't answering: {db_error}"
    elif maintenance is not None:
        row.state = YELLOW
        row.reason = "Maintenance mode is on: workers wait." + (f" {maintenance}" if maintenance else "")
    return row


# -- Processing -----------------------------------------------------------------


def processing_row(*, at: datetime | None, now: datetime, pause_state: str = "running",
                   paused: Sequence[str] = (), failing: int = 0, unreadable: bool = False) -> Row:
    """Red when the scheduler is silent past 30 seconds, or every pause switch
    is on (pause_state "paused", GET /v1/producers/queue's state); yellow
    when some are ("partly"; paused: their titles) or clips failed in the
    last day; green ("Running.") otherwise. The words are Settings →
    Processing's: Running, Partly paused, Paused."""
    row = Row("processing", "Processing", GREEN, "Running.", link="/settings/processing", checked_at=at)
    if unreadable:
        row.state, row.reason = YELLOW, "Couldn't read the scheduler's status."
        return row
    if at is None:
        row.state, row.reason = RED, "The scheduler has never run: nothing is processed."
        return row
    if now - at >= SCHEDULER_SILENT:
        row.state, row.reason = RED, f"The scheduler hasn't reported for {ago(now - at)}: nothing new is processed."
        return row
    if pause_state == "paused":
        row.state, row.reason = RED, "Paused: nothing new is processed until an admin resumes it."
        return row
    notes = []
    if pause_state == "partly":
        notes.append(f"Partly paused: {_names(paused)}." if paused else "Partly paused.")
    if failing:
        notes.append(f"{_clips(failing)} failed in the last day.")
    if notes:
        row.state, row.reason = YELLOW, " ".join(notes)
    return row


# -- AI machines ----------------------------------------------------------------


def ai_row(*, machines: Sequence[Mapping[str, Any]], job_models: Mapping[str, str], starved: set[str],
           now: datetime, scheduler_live: bool = True) -> Row:
    """Over the jobs that are on (a model chosen) and the enabled machines doing them.

    machines: {name, jobs, enabled, online (None: never checked), error, checked_at}.
    starved: jobs with work that no machine can do now (the scheduler's
    eta, "no_machine"). Red for those; yellow when a machine is offline,
    hasn't been checked in MACHINE_STALE or ever, or a job has no machine;
    green when every one is online. With the scheduler down nothing checks
    them: one line says so rather than one per machine."""
    row = Row("ai", "AI machines", GREEN, "", link="/settings/ai")
    on = [job for job in JOBS if job_models.get(job)]
    if not on:
        row.reason = "No AI jobs are on."
        return row
    red = [JOBS[job].lower() for job in on if job in starved]
    if red:
        row.state = RED
        row.reason = f"No machine can do {_names(red)}: work waits."
        return row
    doing = [m for m in machines if m.get("enabled") and any(j in on for j in m.get("jobs") or [])]
    checked = [m["checked_at"] for m in doing if m.get("checked_at")]
    row.checked_at = min(checked) if checked else None
    notes = [f"No machine does {JOBS[job].lower()}." for job in on
             if not any(job in (m.get("jobs") or []) for m in doing)]
    stale = []
    for m in doing:
        name = m.get("name") or "A machine"
        if m.get("online") is None:
            notes.append(f"{name} hasn't been checked yet.")
        elif not m["online"]:
            notes.append(f"{name} is offline.")
        elif m.get("checked_at") and now - m["checked_at"] >= MACHINE_STALE:
            stale.append(m)
            if scheduler_live:
                notes.append(f"{name} hasn't been checked for {ago(now - m['checked_at'])}.")
    if not scheduler_live and (stale or (row.checked_at and now - row.checked_at >= MACHINE_STALE)):
        notes.append(f"Not checked for {ago(now - row.checked_at)}: the scheduler isn't running."
                     if row.checked_at else "Not checked: the scheduler isn't running.")
    if notes:
        row.state, row.reason = YELLOW, " ".join(notes)
    else:
        row.reason = "The machine is online." if len(doing) == 1 else f"All {len(doing)} machines online."
    return row


# -- Search ---------------------------------------------------------------------


def search_row(*, enabled: bool, fallback_on: bool, quickwit: str | None, quickwit_error: str,
               last_fallback: tuple[datetime, str] | None, last_failure: tuple[datetime, str] | None,
               unsynced: int, now: datetime) -> Row:
    """quickwit: "ok", "missing" (the account's index isn't there), "down"
    (not answering), None (off). Red when searches failed lately, or
    Quickwit can't answer and the Postgres fallback is off (searches find
    nothing). Yellow while Postgres stands in (Quickwit off, down or the
    index missing; or a search fell back lately) or many clips wait to be
    indexed. Green when Quickwit answers."""
    row = Row("search", "Search", GREEN, "Quickwit is answering.", checked_at=now)
    if last_failure and now - last_failure[0] < SEARCH_RECENT:
        row.state, row.reason = RED, f"A search failed {ago(now - last_failure[0])} ago: {last_failure[1]}"
        return row
    if not enabled or quickwit in ("down", "missing"):
        why = ("Quickwit is off" if not enabled else
               "Quickwit isn't answering" if quickwit == "down" else "The search index is missing")
        if not fallback_on:
            row.state, row.reason = RED, f"{why} and the Postgres fallback is off: searches find nothing."
            return row
        row.state = YELLOW
        if quickwit == "missing" and enabled:
            row.reason = (f"{why}: search uses Postgres (simpler matching) until upkeep remakes it, "
                          "within 5 minutes, and indexes clips, scenes and transcripts again.")
        else:
            detail = f" ({quickwit_error})" if quickwit_error and enabled and quickwit == "down" else ""
            row.reason = f"{why}{detail}: search uses Postgres (simpler matching)."
        return row
    notes = []
    if last_fallback and now - last_fallback[0] < SEARCH_RECENT:
        notes.append(f"A search fell back to Postgres {ago(now - last_fallback[0])} ago: {last_fallback[1]}")
    if unsynced >= UNSYNCED_YELLOW:
        notes.append(f"{_clips(unsynced)} aren't in the search index yet: search can miss them.")
    if notes:
        row.state, row.reason = YELLOW, " ".join(notes)
    return row


# -- Storage --------------------------------------------------------------------


def _last_seen(libraries: Sequence[tuple[str, str]], storage: Mapping[str, Mapping[str, Any]],
               now: datetime) -> str:
    """When each was last seen: 'Media last seen 2 hours ago.', 'Photos last
    seen 2 hours ago, Footage 5 minutes ago.', 'All last seen within 3 hours.'."""
    seen = [(n, (storage.get(i) or {}).get("seen_at")) for i, n in libraries]
    never = [n for n, at in seen if at is None]
    seen = [(n, at) for n, at in seen if at is not None]
    if len(libraries) <= 2:
        parts = [f"{n} {'last seen ' if k == 0 else ''}{ago(now - at)} ago" for k, (n, at) in enumerate(seen)]
        text = (", ".join(parts) + ".") if parts else ""
        if never:
            text = (text + " " if text else "") + f"{_names(never)} never seen."
        return text
    within = f"last seen within {ago(now - min(at for _, at in seen))}." if seen else ""
    if not never:
        return f"All {within}"
    return f"{_names(never)} never seen." + (f" The others {within}" if within else "")


def library_reachable(seen: Mapping[str, Any] | None, *, at: datetime | None, now: datetime,
                      scans_paused: bool) -> bool | None:
    """A library's dot: True when the latest look since the scheduler started
    reached it, False when it found it unreachable, None when that isn't
    known now (not looked at since a restart, Scans paused, or the scheduler
    isn't running)."""
    if at is None or now - at >= SCHEDULER_SILENT or not seen or not seen.get("checked"):
        return None
    if seen.get("away_since"):
        return False
    return None if scans_paused else True


def storage_row(*, libraries: Sequence[tuple[str, str]], storage: Mapping[str, Mapping[str, Any]],
                scans_paused: bool, at: datetime | None, now: datetime) -> Row:
    """libraries: (library_id, name) of those not trashed; storage: the
    scheduler's looks at each, {seen_at, away_since, checked} (checked:
    looked at since it started; otherwise a record from before), as of its
    status at `at`.

    Red when the latest look found one unreachable; yellow
    while Scans are paused (nothing looks, so what's known is stale:
    Proposed), when one hasn't been looked at since the scheduler started,
    or the scheduler isn't running (each saying when it was last seen);
    green when the latest look reached them all."""
    row = Row("storage", "Storage", GREEN, "", link="/libraries", checked_at=at)
    if not libraries:
        row.reason = "No libraries."
        return row

    def rec(i: str) -> Mapping[str, Any]:
        return storage.get(i) or {}

    away = [(i, n) for i, n in libraries if rec(i).get("checked") and rec(i).get("away_since")]
    if at is None or now - at >= SCHEDULER_SILENT:
        row.state = YELLOW
        row.reason = ("Not checked: the scheduler isn't running." if at is None else
                      f"Last checked {ago(now - at)} ago: the scheduler isn't running.")
        if away:
            row.reason += f" Then {_names([n for _, n in away])} couldn't be reached."
        return row
    if away:
        row.state = RED

        def last(i: str) -> str:
            at = rec(i).get("seen_at")
            return f"last seen {ago(now - at)} ago" if at else "never seen"

        if len(away) == 1:
            row.reason = f"Can't reach {away[0][1]}. {last(away[0][0]).capitalize()}."
        else:
            row.reason = "Can't reach " + ", ".join(f"{n} ({last(i)})" for i, n in away) + "."
        if len(away) == 1:
            row.link = f"/libraries/{away[0][0]}/settings"
        return row
    if scans_paused:
        row.state, row.reason = YELLOW, f"Scans paused. {_last_seen(libraries, storage, now)}"
        return row
    unchecked = [(i, n) for i, n in libraries if not rec(i).get("checked")]
    if unchecked:
        row.state, row.reason = YELLOW, f"Not checked yet. {_last_seen(unchecked, storage, now)}"
        return row
    row.reason = f"All {len(libraries)} {'library' if len(libraries) == 1 else 'libraries'} reachable."
    return row


# -- Disk -----------------------------------------------------------------------


def _size(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def disk_row(*, free: int | None, total: int | None, now: datetime, error: str | None = None) -> Row:
    """The data disk (DATA_DIR): red under 5% free, yellow under 15%."""
    row = Row("disk", "Disk", GREEN, "", checked_at=now)
    if error or not total or free is None:
        row.state, row.reason = YELLOW, f"Couldn't read the data disk: {error or 'no size'}"
        return row
    share = free / total
    row.reason = f"{_size(free)} free of {_size(total)} ({share:.0%})."
    if share < DISK_RED:
        row.state = RED
    elif share < DISK_YELLOW:
        row.state = YELLOW
    return row
