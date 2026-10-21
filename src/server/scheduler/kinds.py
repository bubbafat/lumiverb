"""The kinds of job the scheduler runs, and where each ranks (ADR-016 phase 4).

Tiers, highest first (Robert, Oct 9): 1 see it (scans, which make
thumbnails and previews, and probes); 2 prepare (analysis copies, which
transcripts and scenes are made from); 3 find it (the AI and the rest
that makes clips findable); 4 redo stale work: what was made with another
model or settings than now (changing them was the approval), unless an
admin stopped it; a new producer version, or new defaults a release brings,
stop it until an admin resumes it (lineage.hold_new_version,
hold_new_defaults). Within a tier, oldest first.

Each kind uses one pool: the resource its work waits on, as its producer
declares it (src/producers/pools.py for today's). Descriptions, text in
images and scene descriptions share the vision machines' requests at once;
transcripts the transcript machines'.

A producer's kind is its name (ADR-024: `lumiverb.video.transcript`), its
remakes' redo:<name>; the scan's is `scan`, being no producer's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from src.producers.contract import FIND, PREPARE, REDO, SEE  # noqa: F401 — the tiers
from src.producers.live import LiveMap, LiveSeq
from src.producers.names import remakes_of
from src.producers.pools import SCANS
from src.server.scheduler.dispatch import KindSpec
from src.shared.producers import PRODUCERS

# Scans aren't in the database's queue: each account's libraries are
# looked at this often (reported changes; a full scan every 15 minutes
# unless set: LUMIVERB_FULL_SCAN_EVERY_MIN).
SCAN_EVERY_SEC = 30.0


@dataclass(frozen=True)
class Kind:
    name: str
    spec: KindSpec
    artifact: str = ""  # the producer whose work it is (its name); the scan's is none
    # Reads the originals, so only libraries whose storage is reachable now.
    storage: bool = False
    # Makes again what was made with another model or settings (tier 4).
    redo: bool = False

    @property
    def base(self) -> str:
        """The kind whose runner it uses."""
        return self.spec.same_as or self.name


# A kind per producer the scheduler runs (src/producers/<artifact>/, and a
# plug-in's work: added while it runs), and the scan.
def _first() -> dict[str, Kind]:
    return {k.name: k for k in (
        Kind("scan", KindSpec(SEE, SCANS.name, retake_after=SCAN_EVERY_SEC)),
        *(Kind(p.artifact, KindSpec(p.tier, p.pool.name, batch=p.batch, per_account=p.pool.per_account,
                                    by_seconds=p.unit == "second"),
               p.artifact, storage=p.storage)
          for p in PRODUCERS.values() if p.scheduled),
    )}


def _redo(kind: Kind) -> Kind:
    return replace(kind, name=remakes_of(kind.name), redo=True,
                   spec=replace(kind.spec, tier=REDO, same_as=kind.name))


def _redoable(kind: Kind) -> bool:
    from src.server.repository.lineage import redoable

    return bool(kind.artifact) and redoable(kind.artifact)


def _kinds() -> dict[str, Kind]:
    first = _first()
    return {**first, **{remakes_of(k.name): _redo(k) for k in first.values() if _redoable(k)}}


# Each kind by name, and the tables below, as the producers are now
# (src/producers/live.py): the scheduler takes up a kind added (Scheduler.tick).
KINDS: Mapping[str, Kind] = LiveMap(_kinds)

# The kinds the database lists (all but scans).
QUEUED: tuple[Kind, ...] = LiveSeq(lambda: [k for k in KINDS.values() if k.artifact])  # type: ignore[assignment]
# Kinds that need an AI job's machines (Settings → AI), by job: Lumiverb's own
# producers' (an added one uses no AI job).
AI_JOB_KINDS: dict[str, tuple[str, ...]] = {
    job: tuple(k.name for k in KINDS.values() if k.base in {p.artifact for p in PRODUCERS.values() if p.job == job})
    for job in dict.fromkeys(p.job for p in PRODUCERS.values() if p.job and p.scheduled)
}
