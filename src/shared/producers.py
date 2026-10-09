"""Producers and the lineage of what they make (ADR-016 phases 3 and 4).

Every derived artifact is a function of four inputs: the original (its
SHA-256), the producer, the producer's version and the settings that change
its output. Each producer declares itself in its own folder
(src/producers/<artifact>/), for its one artifact kind, with the media it
applies to and its output-affecting settings; this module is the view of
them both sides use, and the lineage rules. Whatever
writes an artifact records those four inputs beside it (its lineage); when
any of them differs from what's registered now, the artifact is stale.

Bump a producer's ``version`` when its code changes what it makes. Change a
setting's default in its folder (its ``Setting``) when the default changes;
the hash follows. Settings an account changes (Settings → Processing) are
stored on the server and laid over these defaults. An AI
producer's model is its job's (Settings → AI, src/shared/ai_jobs.py), and
only the job's: one model per job.

Shared by the server (what's current) and the worker (what it makes and
records), so they agree on both.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from src.producers import registry
from src.producers.clip import CLIP_MODEL_ID  # noqa: F401 — the vectors' model_id
from src.producers.contract import ALL, IMAGE, VIDEO, ProducerSpec  # noqa: F401
from src.producers.prompts import (  # noqa: F401 — output-affecting settings
    OCR_PROMPT,
    VISION_PROMPT,
)

# A producer, as each one declares itself (src/producers/<artifact>/).
Producer = ProducerSpec

PRODUCERS: dict[str, Producer] = registry()
ARTIFACTS: tuple[str, ...] = tuple(PRODUCERS)

# Pause switches (Robert, Oct 9): one per processing action, each paused
# and resumed on its own: Scans (finding files, thumbnails, video previews),
# Upkeep (the trash purge, file cleanup, spreading face names) and each
# producer the scheduler makes. Pause all pauses every switch; nothing stores
# it. The account's state is derived from the switches (pause_state). "all"
# is named, never inferred from a missing target (Robert, Oct 9).
PAUSE_ALL = "all"
PAUSE_SCANS = "scans"
PAUSE_UPKEEP = "upkeep"
assert not {PAUSE_ALL, PAUSE_SCANS, PAUSE_UPKEEP} & set(PRODUCERS), "a producer can't be named all, scans or upkeep"


def pause_targets() -> tuple[str, ...]:
    """Every pause switch: Scans, Upkeep, then each producer the scheduler makes."""
    return (PAUSE_SCANS, PAUSE_UPKEEP, *(a for a, p in PRODUCERS.items() if p.scheduled))


def pause_state(paused: set[str] | frozenset[str]) -> str:
    """From the switches paused: "running" (none: green), "partly" (some:
    yellow) or "paused" (all: red). A row for what's no longer a switch counts
    for nothing, and a switch added since everything was paused runs."""
    targets = pause_targets()
    n = sum(1 for t in targets if t in paused)
    return "running" if n == 0 else "paused" if n == len(targets) else "partly"

# The repair summary's counts and page filters (what the scheduler is
# handed) and the artifact each is about.
MISSING_FLAGS: dict[str, str] = {p.flag: p.artifact for p in PRODUCERS.values() if p.flag}

# Lineage a write carries when nothing says who made it (an old client, the
# macOS app today): never current, so the brain makes it again.
UNKNOWN = "unknown"
# A person, not a producer, made it (a transcript typed or pasted): current
# whatever the producer's settings, never regenerated over.
PERSON = "person"


def settings_hash(settings: Mapping[str, Any]) -> str:
    """A short, stable hash of output-affecting settings (key order doesn't matter)."""
    canonical = json.dumps(dict(settings), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def effective_settings(artifact: str, overrides: Mapping[str, Any] | None = None,
                       account: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The producer's defaults, then explicit overrides, then its job's model
    from the account ({job: model}; a job without one leaves the default).
    Unknown keys in overrides are ignored: only declared settings change
    output. A job's model comes from the job alone: an override can't say another."""
    p = PRODUCERS[artifact]
    out = dict(p.defaults)
    for key, value in (overrides or {}).items():
        if key in p.defaults and not (p.job and key == "model"):
            out[key] = value
    if p.job and account and account.get(p.job):
        out["model"] = account[p.job]
    return out


def use_settings(artifact: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The producer's settings that don't remake (not in lineage): their
    defaults, then explicit overrides."""
    p = PRODUCERS[artifact]
    out = dict(p.use_defaults)
    for key, value in (overrides or {}).items():
        if key in out:
            out[key] = value
    return out


def lineage(artifact: str, settings: Mapping[str, Any], source_sha256: str | None) -> dict[str, Any]:
    """What a write records about how the artifact was made."""
    p = PRODUCERS[artifact]
    return {
        "producer": p.producer,
        "version": p.version,
        "settings_hash": settings_hash(settings),
        "source_sha256": source_sha256,
    }
