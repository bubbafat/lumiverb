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
from src.producers.live import LiveMap, LiveSeq
from src.producers.prompts import (  # noqa: F401 — output-affecting settings
    OCR_PROMPT,
    VISION_PROMPT,
)

# A producer, as each one declares itself (src/producers/<artifact>/).
Producer = ProducerSpec

# Every producer by its artifact, as the list is now (a plug-in's work can be
# added while Lumiverb runs: src/producers/__init__.py). These and the tables
# below are worked out again when it changes (src/producers/live.py).
PRODUCERS: Mapping[str, Producer] = LiveMap(registry)
ARTIFACTS: tuple[str, ...] = LiveSeq(registry)  # type: ignore[assignment]

# Pause switches (Robert, Oct 9): one per processing action, each paused
# and resumed on its own: Upkeep (the trash purge, file cleanup, spreading
# face names) and each producer the scheduler makes. Scans (finding files,
# thumbnails, video previews) are paused per library (ADR-018 phase 1: the
# library's own, lineage.pause_scans); "scans" is no switch since, and names
# none. Pause all pauses every switch and every library's scans. The
# account's state is derived from them (pause_state). "all" is named, never
# inferred from a missing target (Robert, Oct 9).
PAUSE_ALL = "all"
PAUSE_SCANS = "scans"
PAUSE_UPKEEP = "upkeep"
assert not {PAUSE_ALL, PAUSE_SCANS, PAUSE_UPKEEP} & set(PRODUCERS), "a producer can't be named all, scans or upkeep"


def pause_targets() -> tuple[str, ...]:
    """Every pause switch: Upkeep, then each producer the scheduler makes."""
    return (PAUSE_UPKEEP, *(a for a, p in PRODUCERS.items() if p.scheduled))


def pause_state(paused: set[str] | frozenset[str], *, libraries_paused: int = 0, libraries: int = 0) -> str:
    """From the switches paused and how many of the account's libraries
    (of libraries) have their scans paused: "running" (nothing: green),
    "partly" (some: yellow) or "paused" (every switch and every library:
    red). A row for what's no longer a switch counts for nothing, and a
    switch added since everything was paused runs."""
    targets = pause_targets()
    n = sum(1 for t in targets if t in paused)
    if n == 0 and libraries_paused == 0:
        return "running"
    return "paused" if n == len(targets) and libraries_paused >= libraries else "partly"

# The repair summary's counts and page filters (what the scheduler is
# handed) and the artifact each is about.
MISSING_FLAGS: Mapping[str, str] = LiveMap(lambda: {p.flag: p.artifact for p in registry().values() if p.flag})
# What a scan makes of each file it reads and uploads with its clip, and
# remakes (src/server/scans.py): the preview and thumbnail, a video's preview.
# Lumiverb's own: an added producer makes nothing at a scan.
MADE_AT_SCAN: tuple[str, ...] = tuple(a for a, p in registry().items() if p.made_at_scan)

# Lineage a write carries when nothing says who made it (an old client, the
# macOS app today): never current, so the server makes it again.
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
    from the account ({job: model}; a job without one leaves the default),
    each notch as what it stands for (resolved). Unknown keys in overrides
    are ignored: only declared settings change output. A job's model comes
    from the job alone: an override can't say another."""
    p = PRODUCERS[artifact]
    out = dict(p.defaults)
    for key, value in (overrides or {}).items():
        if key in p.defaults and not (p.job and key == "model"):
            out[key] = value
    if p.job and account and account.get(p.job):
        out["model"] = account[p.job]
    return resolved(artifact, out)


def resolved(artifact: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """The settings as what they make (ADR-018 decision 6): each notch ("better")
    replaced by the numbers it stands for ({"crf": 28}), which lineage hashes
    and a producer's work reads. A notch it doesn't know is its default's."""
    out = dict(settings)
    for s in PRODUCERS[artifact].settings:
        if s.kind == "notch" and s.key in out:
            chosen = out.pop(s.key)
            out.update(s.means.get(chosen, s.means[s.default]))
    return out


def notches(artifact: str, overrides: Mapping[str, Any] | None = None) -> dict[str, str]:
    """The producer's notches as chosen (its defaults, then overrides): what
    Settings shows, as the settings themselves are what they stand for."""
    out = {}
    for s in PRODUCERS[artifact].settings:
        if s.kind == "notch":
            chosen = (overrides or {}).get(s.key, s.default)
            out[s.key] = chosen if chosen in s.choices else s.default
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


def level_of(artifact: str, uses: Mapping[str, Any] | None = None) -> int | None:
    """The producer's level now (its level setting's value, from uses: its
    settings that don't remake), as its index among the choices, lowest 0;
    None when it has no level setting. A value it doesn't know is the default's."""
    setting = PRODUCERS[artifact].level
    if setting is None:
        return None
    value = (uses or {}).get(setting.key, setting.default)
    return setting.choices.index(value if value in setting.choices else setting.default)


def lineage(artifact: str, settings: Mapping[str, Any], source_hash: str | None,
            level: int | None = None) -> dict[str, Any]:
    """What a write records about how the artifact was made. level: the
    level it was made at, for a producer with a level setting (its default's
    when not given); kept apart from the settings hash."""
    p = PRODUCERS[artifact]
    out = {
        "producer": p.producer,
        "version": p.version,
        "settings_hash": settings_hash(settings),
        "source_hash": source_hash,
    }
    if p.level is not None:
        out["level"] = level_of(artifact) if level is None else level
    return out
