"""What a producer declares (ADR-016 phase 4): the public part of a producer.

A producer makes one kind of artifact for the clips it applies to. It says,
in one place, everything the rest of Lumiverb needs to know about it: what
it makes and at which version, what it's made from, which clips it applies
to and how a clip shows it's made, the settings that change its output,
the resource its work waits on, its tier, whether it can be made again in
place, and the function the scheduler runs. The scheduler, the reconciler,
Settings → Processing and the lineage all read it from the registry
(src/producers/__init__.py); nothing else lists producers.

SQL fragments are conditions on ``active_assets a``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Tiers, highest first (Robert, Oct 9): 1 see it (scans, which make
# thumbnails and previews, and probes); 2 prepare (analysis copies, which
# transcripts and scenes are made from); 3 find it (the AI and the rest that
# makes clips findable); 4 redo what's stale (the scheduler's, not a producer's).
SEE, PREPARE, FIND, REDO = 1, 2, 3, 4

IMAGE = ("image",)
VIDEO = ("video",)
ALL = ("image", "video")


@dataclass(frozen=True)
class ProducerSpec:
    artifact: str  # the one artifact kind it makes
    producer: str  # its id, recorded in lineage
    version: str  # bump when the code changes its output
    media: tuple[str, ...]  # the media types it applies to
    title: str  # for people: "Transcripts"
    applies: str  # which clips it applies to (SQL)
    made: str  # whether a clip has the artifact (SQL); one made in parts once every part is
    defaults: Mapping[str, Any] = field(default_factory=dict)  # output-affecting settings
    # The artifacts it's made from: the queue waits until a clip has them.
    needs: tuple[str, ...] = ()
    # Its output must come from one model across the library (an embedding
    # space): an upgrade can't be partial.
    uniform: bool = False
    # The AI job whose model is its "model" (Settings → AI, src/shared/ai_jobs.py).
    job: str = ""
    # Why it can't be made again in place yet; its stale artifacts wait.
    cant_redo: str = ""
    # Handed out again when its clip's file changes. Not for what's made from
    # parts that would be stale too (scenes and what's made from them).
    redo_on_source_change: bool = True
    # Where it shows in lists (Settings → Processing).
    order: int = 0

    # --- How the scheduler runs it (none of these: made by a scan) ---
    kind: str = ""  # the job kind's name ("render" for analysis copies)
    flag: str = ""  # the repair summary's missing_* filter for it
    run: str = ""  # "module:function", called with (account, job)
    tier: int = FIND
    pool: str = ""  # the resource its work waits on
    batch: int = 1  # clips a job
    per_account: bool = False  # the pool is each account's own (its AI machines)
    storage: bool = False  # reads the originals: only libraries reachable now

    @property
    def scheduled(self) -> bool:
        return bool(self.kind)
