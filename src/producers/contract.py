"""What a producer declares (ADR-016 phase 4): the public part of a producer.

A producer makes one kind of artifact for the clips it applies to. It says,
in one place, everything the rest of Lumiverb needs to know about it: what
it makes and at which version, what it's made from, which clips it applies
to and how a clip shows it's made, the settings that change its output,
the pool its work waits on (and so the AI job whose machines do it), its
tier, whether it can be made again in place and what goes with it then,
and its work (a src/producers/runner.py Work: make and save). Everything
else reads it from the registry (src/producers/__init__.py): the scheduler
(queue, kinds, pools and their slots, GPU sharing, runners), the reconciler
and lineage, the repair summary and the asset page's missing_* filters, the
AI jobs and where each one's model is kept (Settings → AI), and
GET /v1/producers (which Settings → Processing shows as it comes). Adding a
producer is one folder: docs/architecture.md, "Adding a producer".

A producer's ``__init__`` only declares: it imports this module (and
src/producers/prompts.py, src/producers/pools.py), never what imports the
registry (src/shared/producers.py, the server, processing), or it would be
found half-loaded. Its work lives in a module of its own (work.py),
imported when the scheduler first needs it, and may import anything.

SQL fragments are conditions on ``active_assets a``.
"""

from __future__ import annotations

import math
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
    """A producer's setting, as Settings → Processing shows it (bounds,
    advanced or not). An output-affecting one (remakes) goes into the
    lineage hash; one that only changes what's done with the artifacts
    made (how faces are grouped) doesn't, and its producer's ``regroup``
    runs when it changes."""

    key: str
    default: Any
    label: str
    kind: str = "int"  # "int" | "float" | "text" | "bool"
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    advanced: bool = False  # folded away until asked for
    # Why it can't be changed here ("" = it can): only what the producer's
    # code reads is offered, or lineage would claim settings nothing used.
    fixed: str = ""
    # Changing it makes the artifact again (it's in lineage). False: it
    # changes only what's done with what's made; nothing is made again.
    remakes: bool = True

    def check(self, value: Any) -> Any:
        """The value as stored, or ValueError saying what's wrong with it."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.label} is yes or no (true or false)")
            return value
        if self.kind == "text":
            if not isinstance(value, str):
                raise ValueError(f"{self.label} is text")
            if not value.strip():
                raise ValueError(f"{self.label} can't be empty")
            if "\x00" in value or len(value) > 4000:
                raise ValueError(f"{self.label} is text, up to 4,000 characters")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.label} is a number")
        try:
            number = float(value)
        except OverflowError:  # a whole number too big for a float
            number = math.inf
        # NaN and infinity are within no bounds (JSON parsers take them).
        if not math.isfinite(number) or (self.minimum is not None and number < self.minimum) \
                or (self.maximum is not None and number > self.maximum):
            raise ValueError(self.bounds())
        if self.kind == "int":
            if not number.is_integer():
                raise ValueError(f"{self.label} is a whole number")
            return int(number)
        return number

    def bounds(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        if self.minimum is None or self.maximum is None:
            return f"{self.label} is a finite number"
        return f"{self.label} is from {self.minimum:g} to {self.maximum:g}{unit}"


@dataclass(frozen=True)
class AiJob:
    """Work the account's AI machines do (Settings → AI): one model for the
    account, kept by the job's name (the tenant's ai_job_models); a machine
    does the job only while it offers that model. Producers sharing the
    machines name the same job (through their pool)."""

    name: str  # "vision"
    label: str  # what Settings calls it: "Descriptions & text"
    # "module:Class", made with (client): checks the machines and calls them
    # (src/processing/job_guard.py). It's how the job's machines are asked.
    guard: str
    # The model a new account starts with ("" = the job is off until one is chosen).
    default_model: str = ""
    # The scheduler's own machine can do it (transcripts: its Whisper).
    built_in: bool = False
    # Jobs handed out for each request the machines take at once: transcripts
    # 2, so each clip's audio is got ready while the machines hear others
    # (the machines hold their own limits).
    per_request: int = 1


# GPU sharing: while a pool's jobs run, AI machines sharing this machine's
# GPU take fewer requests (video work comes first, Robert, Oct 9).
DECODES = "decodes"  # one per job decoding on the GPU, up to the machine's GPU decodes
WHILE_RUNNING = "while_running"  # one while any of its jobs runs


@dataclass(frozen=True)
class Pool:
    """The resource a producer's work waits on: so many jobs at once. Pools
    are shared by name; producers naming one declare it alike."""

    name: str
    slots: int = 1  # jobs at once
    # This machine's setting (src/processing/machine.py) that sizes it instead
    # ("renders": analysis proxies rendered at once here).
    sized_by: str = ""
    # Each account's own: its AI job's machines size it (Settings → AI).
    job: AiJob | None = None
    gpu_hold: str = ""  # DECODES | WHILE_RUNNING (see above)

    @property
    def per_account(self) -> bool:
        return self.job is not None


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
    # Why it can't be made again in place yet; its stale artifacts wait.
    cant_redo: str = ""
    # What's made from it and goes when it's made again (made anew after it):
    # their lineage goes with its own (lineage.start_over), and the 409
    # before new settings names them.
    redo_also: tuple[str, ...] = ()
    # What making it again keeps, in plain words: the question before new settings says it.
    redo_note: str = ""
    # Handed out again when its clip's file changes. Not the analysis copy
    # and what's made from it: a file with other content is a new clip
    # (#37), so a change in place isn't a reason to find scenes again.
    redo_on_source_change: bool = True
    # Where it shows in lists (Settings → Processing); one without sorts last.
    order: int = 1000
    # "module:function", called with (session) when a setting that doesn't
    # remake changes (faces: the face groups are worked out again).
    regroup: str = ""

    # --- How the scheduler runs it (none of these: made by a scan) ---
    kind: str = ""  # the job kind's name ("render" for analysis copies)
    # Its missing_* filter (the asset page) and repair summary count.
    flag: str = ""
    run: str = ""  # its work: "module:Class", a src/producers/runner.py Work
    tier: int = FIND
    pool: Pool | None = None  # the resource its work waits on
    batch: int = 1  # clips a job
    storage: bool = False  # reads the originals: only libraries reachable now
    # What its work grows with, for how long is left: "second" (of video:
    # a render, a transcript) or "clip" (a photo described).
    unit: str = "clip"

    @property
    def scheduled(self) -> bool:
        return bool(self.kind)

    @property
    def job(self) -> str:
        """The AI job whose model is its "model" (Settings → AI): its pool's, if any."""
        return self.pool.job.name if self.pool is not None and self.pool.job is not None else ""

    @property
    def defaults(self) -> Mapping[str, Any]:
        """The output-affecting settings' defaults: what lineage hashes."""
        return {s.key: s.default for s in self.settings if s.remakes}

    @property
    def use_defaults(self) -> Mapping[str, Any]:
        """The defaults of settings that don't remake (what's done with what's made)."""
        return {s.key: s.default for s in self.settings if not s.remakes}

    def setting(self, key: str) -> Setting | None:
        return next((s for s in self.settings if s.key == key), None)
