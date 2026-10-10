"""The kinds of job the scheduler runs, and where each ranks (ADR-016 phase 4).

Tiers, highest first (Robert, Oct 9): 1 see it (scans, which make
thumbnails and previews, and probes); 2 prepare (analysis copies, which
transcripts and scenes are made from); 3 find it (the AI and the rest
that makes clips findable); 4 redo stale work: what was made with another
model or settings than now (changing them was the approval), unless an
admin stopped it; a new producer version stops it until an admin resumes
it (lineage.hold_new_version). Within a tier, oldest first.

Each kind uses one pool: the resource its work waits on, as its producer
declares it (src/producers/pools.py for today's). Descriptions, text in
images and scene descriptions share the vision machines' requests at once;
transcripts the transcript machines'.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.producers.contract import FIND, PREPARE, REDO, SEE  # noqa: F401 — the tiers
from src.producers.pools import SCANS
from src.server.scheduler.dispatch import KindSpec
from src.shared.producers import MISSING_FLAGS, PRODUCERS

# Scans aren't in the database's queue: each account's libraries are
# looked at this often (reported changes; a full scan once a day).
SCAN_EVERY_SEC = 30.0


@dataclass(frozen=True)
class Kind:
    name: str
    spec: KindSpec
    flag: str = ""  # its missing_* condition (repository/tenant.py MISSING_CONDITIONS)
    extra: str = "true"  # what else a clip needs first (SQL on active_assets a)
    # Reads the originals, so only libraries whose storage is reachable now.
    storage: bool = False
    # Makes again what was made with another model or settings (tier 4).
    redo: bool = False

    @property
    def artifact(self) -> str:
        return MISSING_FLAGS[self.flag]

    @property
    def base(self) -> str:
        """The kind whose runner it uses."""
        return self.spec.same_as or self.name


def _made_first(needs: tuple[str, ...]) -> str:
    """What a clip needs first: the artifacts the producer is made from."""
    from src.server.repository.lineage import MADE

    return " AND ".join(f"({MADE[n]})" for n in needs) or "true"


# A kind per producer the scheduler runs (src/producers/<artifact>/), and the scan.
_FIRST: dict[str, Kind] = {k.name: k for k in (
    Kind("scan", KindSpec(SEE, SCANS.name, retake_after=SCAN_EVERY_SEC)),
    *(Kind(p.kind, KindSpec(p.tier, p.pool.name, batch=p.batch, per_account=p.pool.per_account,
                            by_seconds=p.unit == "second"),
           p.flag, _made_first(p.needs), storage=p.storage)
      for p in PRODUCERS.values() if p.scheduled),
)}


def _redo(kind: Kind) -> Kind:
    return replace(kind, name=f"redo_{kind.name}", redo=True,
                   spec=replace(kind.spec, tier=REDO, same_as=kind.name))


def _redoable(kind: Kind) -> bool:
    from src.server.repository.lineage import redoable

    return bool(kind.flag) and redoable(kind.artifact)


KINDS: dict[str, Kind] = {**_FIRST, **{f"redo_{k.name}": _redo(k) for k in _FIRST.values() if _redoable(k)}}

# The kinds the database lists (all but scans).
QUEUED = tuple(k for k in KINDS.values() if k.flag)
# Kinds that need an AI job's machines (Settings → AI), by job.
AI_JOB_KINDS: dict[str, tuple[str, ...]] = {
    job: tuple(k.name for k in KINDS.values() if k.base in {p.kind for p in PRODUCERS.values() if p.job == job})
    for job in dict.fromkeys(p.job for p in PRODUCERS.values() if p.job and p.scheduled)
}
