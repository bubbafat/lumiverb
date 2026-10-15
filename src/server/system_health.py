"""System health for the Admin page: seven rows, each green, yellow or red
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
# This many clips not searchable yet (their search rows not written): search can miss them. Proposed.
UNSYNCED_YELLOW = 100
# Free space on the data disk, as a share of it. Proposed.
DISK_YELLOW = 0.15
DISK_RED = 0.05
# No backup for this long is red (one is made every night). Proposed.
BACKUP_RED = timedelta(days=2)


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
    when some are ("partly"; paused: their titles, a library's scans as
    "Scans of <library>") or clips failed in the
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


def search_row(*, unsynced: int, now: datetime) -> Row:
    """Search is in the account's database (ADR-019): there's nothing to be
    down. Yellow while many clips wait for the search sync to write their
    words (upkeep does, within minutes); a few waiting is said, and green."""
    row = Row("search", "Search", GREEN, "Every clip is searchable.", checked_at=now)
    if unsynced:
        row.reason = (f"{_clips(unsynced)} {'isn' if unsynced == 1 else 'aren'}'t searchable yet: "
                      f"upkeep adds {'it' if unsynced == 1 else 'them'} within minutes.")
    if unsynced >= UNSYNCED_YELLOW:
        row.state = YELLOW
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
                      stopped: bool) -> bool | None:
    """A library's dot: True when the latest look since the scheduler started
    reached it, False when it found it unreachable, None when that isn't
    known now (not looked at since a restart, all processing paused, or the
    scheduler isn't running)."""
    if stopped or at is None or now - at >= SCHEDULER_SILENT or not seen or not seen.get("checked"):
        return None
    return not seen.get("away_since")


def storage_row(*, libraries: Sequence[tuple[str, str]], storage: Mapping[str, Mapping[str, Any]],
                stopped: bool, at: datetime | None, now: datetime) -> Row:
    """libraries: (library_id, name) of those not trashed; storage: the
    scheduler's looks at each, {seen_at, away_since, checked} (checked:
    looked at since it started; otherwise a record from before), as of its
    status at `at`; stopped: Pause all is on (lineage.Pauses.everything).

    The scheduler looks at each library's storage itself every 30 seconds,
    whatever is paused (pausing Scans changes nothing here), except while
    all processing is paused: nothing touches the drives then. Yellow
    while the scheduler isn't running or all processing is paused (what's
    known is stale, not broken), or when one hasn't been looked at since
    the scheduler started, each saying when it was last seen; red when the
    latest look found one unreachable; green when it reached them all."""
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
    if stopped:
        row.state = YELLOW
        row.reason = f"All processing paused, so storage isn't checked. {_last_seen(libraries, storage, now)}"
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


def _space(free: int, total: int) -> tuple[str, str]:
    share = free / total
    state = RED if share < DISK_RED else YELLOW if share < DISK_YELLOW else GREEN
    return state, f"{_size(free)} free of {_size(total)} ({share:.0%})"


def disk_row(*, free: int | None, total: int | None, now: datetime, error: str | None = None,
             previews: Mapping[str, Any] | None = None, unwritable: Sequence[str] = ()) -> Row:
    """The data disk (DATA_DIR) and, when they're elsewhere, the previews'
    (`previews`: folder, available, free, total, same_disk, moving: share
    moved, missing: libraries' own folders whose drive is unplugged): red
    under 5% free, a previews drive unplugged or a previews folder Lumiverb
    can't write (`unwritable`: why, src/server/write_check.py), yellow
    under 15%."""
    row = Row("disk", "Disk", GREEN, "", checked_at=now)
    if error or not total or free is None:
        row.state, row.reason = YELLOW, f"Couldn't read the data disk: {error or 'no size'}"
        return row
    row.state, said = _space(free, total)
    row.reason = f"{said}."
    if previews:
        moving = previews.get("moving")
        doing = f" Moving the previews: {moving:.0%}." if moving is not None else ""
        if not previews.get("available"):
            row.state, row.link = RED, "/settings/storage"
            row.reason = f"The previews folder isn't there: {previews.get('folder')}. Is its drive plugged in?"
            return row
        if previews.get("missing"):
            row.state, row.link = RED, "/settings/storage"
            row.reason = (f"A library's previews folder isn't there: {', '.join(previews['missing'])}. "
                          "Is its drive plugged in?")
            return row
        if not previews.get("same_disk") and previews.get("total"):
            state, theirs = _space(int(previews.get("free") or 0), int(previews["total"]))
            row.reason = f"Data: {said}. Previews: {theirs}.{doing}"
            row.state = max(row.state, state, key=_RANK.__getitem__)
            row.link = "/settings/storage"
        elif doing:
            row.reason += doing
    if unwritable:
        row.state, row.link = RED, "/settings/storage"
        row.reason = " ".join(unwritable)
    return row


# -- Backups ---------------------------------------------------------------------


def backups_row(*, newest: datetime | None, newest_bytes: int = 0, last_error: str | None = None,
                last_failed_at: datetime | None = None, running: Mapping[str, Any] | None = None,
                folder_error: str | None = None, now: datetime) -> Row:
    """The databases' backups (src/server/backups.py): red with none for 2
    days or a folder Lumiverb can't write (`folder_error`: why,
    src/server/write_check.py), yellow when the last one failed or none is
    made yet."""
    row = Row("backups", "Backups", GREEN, "", link="/settings/backups", checked_at=now)
    if folder_error:
        row.state, row.reason = RED, f"{folder_error} The next backup can't be made."
        return row
    failed = bool(last_error) and last_failed_at is not None and (newest is None or last_failed_at > newest)
    doing = ""
    if running:
        of = int(running.get("of") or 0)
        doing = f" Backing up now ({int(running.get('done') or 0) + 1} of {of})." if of else " Backing up now."
    if newest is None:
        if failed:
            row.state, row.reason = RED, f"No backup yet: {last_error}"
        else:
            row.state = YELLOW
            row.reason = ("Making the first backup." if running else "No backup yet: the first is made within minutes.")
        return row
    age = now - newest
    row.reason = f"Last backup {ago(age)} ago ({_size(newest_bytes)}).{doing}"
    if age > BACKUP_RED:
        row.state = RED
        row.reason = f"No backup for {ago(age)}" + (f": {last_error}" if failed else ".") + doing
    elif failed:
        row.state = YELLOW
        row.reason = f"The last backup failed: {last_error} The one before is {ago(age)} old.{doing}"
    return row
