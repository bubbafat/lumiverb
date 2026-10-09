"""The kinds of job the scheduler runs, and where each ranks (ADR-016 phase 4).

Tiers, highest first (Robert, Oct 9): 1 see it (scans, which make
thumbnails and previews, and probes); 2 prepare (analysis copies, which
transcripts and scenes are made from); 3 find it (the AI and the rest
that makes clips findable); 4 redo stale work: what was made with another
model or settings than now (changing them was the approval), unless an
admin stopped it. Within a tier, oldest first.

Each kind uses one pool: the resource its work waits on. Descriptions,
text in images and scene descriptions share the vision machines' requests
at once; transcripts the transcript machines'.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from src.server.scheduler.dispatch import KindSpec
from src.shared.producers import MISSING_FLAGS

SEE, PREPARE, FIND, REDO = 1, 2, 3, 4

# Scans aren't in the database's queue: each account's libraries are
# looked at this often (reported changes; a full scan once a day).
SCAN_EVERY_SEC = 30.0

_HAS_PROXY = "a.proxy_key IS NOT NULL"
_HAS_ANALYSIS_PROXY = "a.analysis_proxy_key IS NOT NULL"


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


_FIRST: dict[str, Kind] = {k.name: k for k in (
    Kind("scan", KindSpec(SEE, "scan", retake_after=SCAN_EVERY_SEC)),
    Kind("probe", KindSpec(SEE, "probe"), "missing_probe", storage=True),
    Kind("render", KindSpec(PREPARE, "render"), "missing_analysis_proxy", storage=True),
    # CLIP and face detection take turns on this machine's GPU.
    Kind("clip", KindSpec(FIND, "gpu"), "missing_embeddings", _HAS_PROXY),
    # The AI machines are each account's own (Settings → AI).
    Kind("vision", KindSpec(FIND, "vision", per_account=True), "missing_vision", _HAS_PROXY),
    Kind("ocr", KindSpec(FIND, "vision", per_account=True), "missing_ocr", _HAS_PROXY),
    Kind("scene_vision", KindSpec(FIND, "vision", per_account=True), "missing_scene_vision", _HAS_ANALYSIS_PROXY),
    Kind("faces", KindSpec(FIND, "gpu", batch=25), "missing_faces", _HAS_PROXY),
    Kind("transcript", KindSpec(FIND, "transcripts", per_account=True), "missing_transcription",
         _HAS_ANALYSIS_PROXY),
    Kind("scenes", KindSpec(FIND, "scenes"), "missing_video_scenes", _HAS_ANALYSIS_PROXY),
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
    job: tuple(k.name for k in KINDS.values() if k.base in bases)
    for job, bases in {"vision": ("vision", "ocr", "scene_vision"), "transcripts": ("transcript",)}.items()
}
