"""Why a producer's due work waits (Robert, Oct 12: no silent waits).

Work is due when the scheduler would hand it out: what's missing, and
what's stale while its redo runs. Each thing that can hold due work back
has a code here, and every one is said: GET /v1/producers lists, on each
producer's row, the codes holding some of its work now and how many clips
(src/server/producer_waits.py), GET /v1/producers/queue each library's
scans' on its row (scan_waits there), and Settings → Processing has a sentence for
each (src/ui/web/src/lib/waits.ts). The scheduler names what it holds back
itself (a job's clips that wait after a try: Waits' reason, Waiting) with
these codes. Tests hold the scheduler, the API and the web to this list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# Its switch is paused, or all processing is.
PAUSED = "paused"
# The scheduler isn't running, is starting (it hasn't read the account's
# settings yet), or couldn't list the producer's work.
SCHEDULER = "scheduler"
# Its AI job has no machine the scheduler can use: no model chosen, no
# machine doing it, or none online (Settings → AI).
AI_MACHINES = "ai_machines"
# What it's made from isn't made yet (ProducerSpec.needs).
NEEDS = "needs"
# It reads the originals, and a library's drive can't be reached, or hasn't
# been looked at since the scheduler started.
STORAGE = "storage"
# It reads the originals, and a device reads the library (ADR-018 decision
# 1): the device makes it, from its next check-in.
DEVICE = "device"
# A new version of the producer stopped its redo: what an older version
# made is made again once an admin resumes it.
NEW_VERSION = "new_version"
# An admin stopped its redo: its stale clips stay as they are until resumed.
REDO_STOPPED = "redo_stopped"
# The file wasn't on its storage, or changed since it was hashed: a scan
# comes first (the scheduler holds the clip back the while).
SCAN = "scan"
# Its last try couldn't finish, through no fault of the clip's (the GPU ran
# out of memory, the AI machines or the server away, a crash not yet
# charged): the scheduler tries it again within the hour.
HELD = "held"
# Its last try failed: it's tried again after 5 minutes, doubling up to a day.
RETRY_LATER = "retry_later"
# It failed so many times it's no longer tried, until someone asks.
GIVEN_UP = "given_up"
# A library's scans' (Scans, each library's row): they keep failing (the
# scan itself, not a file): tried again after a minute, doubling up to half an hour.
SCAN_FAILING = "scan_failing"

# Every code, in the order a row shows them, with what it means.
REASONS: dict[str, str] = {
    PAUSED: "its switch is paused, or all processing is",
    SCHEDULER: "the scheduler isn't running, is starting, or couldn't list its work",
    AI_MACHINES: "its AI job has no machine the scheduler can use",
    NEEDS: "what it's made from isn't made yet",
    STORAGE: "a library's drive can't be reached, or hasn't been looked at since the scheduler started",
    DEVICE: "a device reads the library: the device makes it from its originals",
    NEW_VERSION: "a new version of it waits for an admin to resume its redo",
    REDO_STOPPED: "an admin chose to keep what it made another way than now as it is (new files only, or Stop)",
    SCAN: "its file wasn't on its storage, or changed: a scan comes first",
    HELD: "its last try couldn't finish through no fault of the clip's: tried again within the hour",
    RETRY_LATER: "its last try failed: tried again later",
    GIVEN_UP: "it failed too many times: tried again when someone asks",
    SCAN_FAILING: "a library's scans keep failing: tried again later, a longer while each time",
}


class Waiting(list):
    """A job's clips that wait, neither saved nor charged (their asset ids,
    in order: a list, as a job's outcome always was), each with why (a code
    above; HELD when it isn't said)."""

    def __init__(self, asset_ids: Iterable[str] = (), why: Mapping[str, str] | None = None) -> None:
        super().__init__(asset_ids)
        self.why: dict[str, str] = {i: r for i, r in (why or {}).items() if r in REASONS}

    def add(self, asset_id: str, reason: str = HELD) -> None:
        self.append(asset_id)
        self.why[asset_id] = reason if reason in REASONS else HELD

    def reason(self, asset_id: str) -> str:
        return self.why.get(asset_id, HELD)


def reasons_of(waiting: Iterable[str] | None) -> dict[str, str]:
    """Why each of these clips waits: a Waiting says; anything else is HELD."""
    if waiting is None:
        return {}
    why = getattr(waiting, "why", None) or {}
    return {i: why.get(i, HELD) for i in waiting}
