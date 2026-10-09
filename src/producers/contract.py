"""What a producer declares (ADR-016 phase 4): the public part of a producer.

A producer makes one kind of artifact for the clips it applies to. It says,
in one place, everything the rest of Lumiverb needs to know about it: what
it makes and at which version, what it's made from, which clips it applies
to and how a clip shows it's made, the settings that change its output,
the resource its work waits on, its tier, whether it can be made again in
place, and the function the scheduler runs. The scheduler (its queue, kinds, pools
and runners), the reconciler, the lineage and GET /v1/producers (which
Settings → Processing shows as it comes) read it from the registry
(src/producers/__init__.py). Still named by hand: the repair summary's
counts and the asset page's missing_* filters (the older enrich flow), and
a new AI job's machines (src/shared/ai_jobs.py and its guard).

A producer's ``__init__`` only declares: it imports this module (and
src/producers/prompts.py), never what imports the registry
(src/shared/producers.py, the server, the workers), or it would be found
half-loaded. Its ``run`` function lives in a module of its own, imported
when the scheduler first needs it, and may import anything.

SQL fragments are conditions on ``active_assets a``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Tiers, highest first (Robert, Oct 9): 1 see it (scans, which make
# thumbnails and previews, and probes); 2 prepare (analysis copies, which
# transcripts and scenes are made from); 3 find it (the AI and the rest that
# makes clips findable); 4 redo what's stale (the scheduler's, not a producer's).
SEE, PREPARE, FIND, REDO = 1, 2, 3, 4

IMAGE = ("image",)
VIDEO = ("video",)
ALL = ("image", "video")


# Why a setting can't be changed here.
ITS_JOBS = "Chosen in Settings → AI, for every producer its machines run."
NOT_READ_YET = "Not read by this producer yet: changing it would change nothing it makes."


@dataclass(frozen=True)
class Setting:
    """An output-affecting setting: its default goes into the lineage hash,
    and Settings → Processing shows it from this (bounds, advanced or not)."""

    key: str
    default: Any
    label: str
    kind: str = "int"  # "int" | "float" | "text"
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    advanced: bool = False  # folded away until asked for
    # Why it can't be changed here ("" = it can): only what the producer's
    # code reads is offered, or lineage would claim settings nothing used.
    fixed: str = ""

    def check(self, value: Any) -> Any:
        """The value as stored, or ValueError saying what's wrong with it."""
        if self.kind == "text":
            if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 4000:
                raise ValueError(f"{self.label} is text, up to 4,000 characters")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.label} is a number")
        if self.kind == "int":
            if value != int(value):
                raise ValueError(f"{self.label} is a whole number")
            value = int(value)
        else:
            value = float(value)
        if (self.minimum is not None and value < self.minimum) or (self.maximum is not None and value > self.maximum):
            raise ValueError(f"{self.label} is from {self.minimum:g} to {self.maximum:g}{(' ' + self.unit) if self.unit else ''}")
        return value


@dataclass(frozen=True)
class ProducerSpec:
    artifact: str  # the one artifact kind it makes
    producer: str  # its id, recorded in lineage
    version: str  # bump when the code changes its output
    media: tuple[str, ...]  # the media types it applies to
    title: str  # for people: "Transcripts"
    applies: str  # which clips it applies to (SQL)
    made: str  # whether a clip has the artifact (SQL); one made in parts once every part is
    settings: tuple[Setting, ...] = ()  # output-affecting
    # The artifacts it's made from: the queue waits until a clip has them.
    needs: tuple[str, ...] = ()
    # Its output must come from one model across the library (an embedding
    # space): an upgrade can't be partial.
    uniform: bool = False
    # The AI job whose model is its "model" (Settings → AI, src/shared/ai_jobs.py).
    job: str = ""
    # Why it can't be made again in place yet; its stale artifacts wait.
    cant_redo: str = ""
    # Handed out again when its clip's file changes. Not the analysis copy
    # and what's made from it: scenes can't be found again in place, and a
    # new copy would leave them describing the old file.
    redo_on_source_change: bool = True
    # Where it shows in lists (Settings → Processing); one without sorts last.
    order: int = 1000

    # --- How the scheduler runs it (none of these: made by a scan) ---
    kind: str = ""  # the job kind's name ("render" for analysis copies)
    flag: str = ""  # the repair summary's missing_* filter for it
    run: str = ""  # "module:function", called with (account, job)
    tier: int = FIND
    pool: str = ""  # the resource its work waits on
    batch: int = 1  # clips a job
    # The pool is each account's own: its AI job's machines (the pool is the job's name).
    per_account: bool = False
    # Jobs at once in its pool, when the scheduler doesn't size the pool
    # itself (scan, probe, render, gpu, scenes) and machines don't (an AI job's).
    slots: int = 1
    storage: bool = False  # reads the originals: only libraries reachable now

    @property
    def scheduled(self) -> bool:
        return bool(self.kind)

    @property
    def defaults(self) -> Mapping[str, Any]:
        return {s.key: s.default for s in self.settings}

    def setting(self, key: str) -> Setting | None:
        return next((s for s in self.settings if s.key == key), None)
