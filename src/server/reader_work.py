"""What a library's reader has to do: one list, computed (ADR-018 decision 3, phase 3).

Only a library's reader reads its originals: this server for a library on
its own disks (or a share it mounts), a device for one it reads (decision
1). So whatever needs the originals is the reader's work, and the server
says what that is, every reason in one list:

- previews (PREVIEWS): a clip's preview and thumbnail, and a video's
  10-second preview, made when its file is scanned. Due when one is
  missing: it never arrived, or it was lost (the missing-file repair cleared
  its key, src/server/missing_artifacts.py). A scan makes them again: its
  plan uploads such a clip's file (src/server/scans.py, reason
  preview_missing), by previews_missing here.
- what each producer that reads the originals makes (storage=True: a
  video's facts, capture facts, analysis proxies), by the reconciler's rule
  (lineage.queued): missing, made from content that has since changed, or
  due to be checked again, less failures waiting their turn; and, while the
  producer's redo runs, made with another version or settings than now (a
  remake). On this server they're the scheduler's producers, whose queue
  lists by the same rule (scheduler/queue.py).

The list is computed each time it's asked, never kept as jobs: settings
that go low, high, low again leave nothing to do, and what's made leaves
the list by itself, since its upload records how it was made (its lineage).

What's paused isn't listed, as the scheduler hands none of it out: no
previews while the library's scans are paused (only a scan makes them,
and Pause all pauses them too); none of a producer's work while its switch
is paused; no remakes while its redo is stopped, or while a new version of
it waits to be seen (the scheduler stops its redo then, until an admin
resumes it: lineage.hold_new_version).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.server.repository import lineage
from src.server.repository.lineage import APPLIES, LINEAGE_JOIN, MADE
from src.shared.producers import MADE_AT_SCAN as SCANNED
from src.shared.producers import PRODUCERS

PREVIEWS = "previews"
# What a scan makes of each file it reads and uploads with its clip: the
# preview and thumbnail (the proxy producer's), a video's 10-second preview.
MADE_AT_SCAN = SCANNED
# What the producers that read the originals make, in the registry's order.
FROM_ORIGINALS = tuple(a for a, p in PRODUCERS.items() if p.storage)
WORK = (PREVIEWS, *FROM_ORIGINALS)


def previews_missing() -> str:
    """SQL on a clip `a` (assets or active_assets): a preview a scan makes is missing."""
    return "(" + " OR ".join(f"(({APPLIES[x]}) AND NOT ({MADE[x]}))" for x in MADE_AT_SCAN) + ")"


@dataclass(frozen=True)
class Listed:
    """What the list holds now: the work not paused, in WORK's order, and
    the producers whose remakes are listed, each with what it makes now
    (lineage.desired): their redo runs."""

    work: tuple[str, ...] = ()
    remakes: dict[str, dict[str, Any]] = field(default_factory=dict)


def listed(session: Session, library_id: str) -> Listed:
    """What's listed for the library now, as the scheduler would hand it out (only reads)."""
    paused = lineage.pauses(session)
    work = tuple(w for w in WORK if not (paused.scans_paused(library_id) if w == PREVIEWS else w in paused.work))
    remakes = {
        # These producers take no AI job's model (only the originals): what they make now is the account's settings.
        a: lineage.desired(session, a) for a in work
        if a != PREVIEWS and lineage.redoable(a) and a not in paused.redo and not lineage.new_version_unseen(session, a)
    }
    return Listed(work, remakes)


def page(session: Session, library_id: str, *, after: str | None, limit: int) -> list[dict]:
    """A page of the library's clips in sight that have work in the list, by
    asset_id (oldest first, as ids are made): each one's path, media type,
    size and content hash; its work (missing, in WORK's order) and its
    remakes (made another way than now: after everything else, decision 6)."""
    now = listed(session, library_id)
    if not now.work:
        return []
    columns = {f"w_{w}": previews_missing() if w == PREVIEWS else lineage.queued(w) for w in now.work}
    columns |= {f"r_{a}": lineage.queued(a, redo=True) for a in now.remakes}
    params: dict[str, Any] = {"lib": library_id, "after": after, "n": limit}
    for a, want in now.remakes.items():
        params |= lineage.redo_params(a, want)
    rows = session.execute(text(
        "SELECT a.asset_id, a.rel_path, a.media_type, a.file_size, a.content_hash, "
        + ", ".join(f"{sql} AS {name}" for name, sql in columns.items())
        + f" FROM active_assets a {LINEAGE_JOIN}"
        " WHERE a.library_id = :lib AND (CAST(:after AS text) IS NULL OR a.asset_id > :after)"
        f"   AND ({' OR '.join(columns.values())})"
        " ORDER BY a.asset_id LIMIT :n"
    ), params).mappings().all()
    return [{"asset_id": r["asset_id"], "rel_path": r["rel_path"], "media_type": r["media_type"],
             "file_size": r["file_size"], "content_hash": r["content_hash"],
             "work": [w for w in now.work if r[f"w_{w}"]],
             "remakes": [a for a in now.remakes if r[f"r_{a}"]]} for r in rows]
