"""The producers: one folder each, found here (ADR-016 phase 4), and those
added while Lumiverb runs (each installed plug-in's work, ADR-023).

Each subpackage of this one is a producer: its ``__init__`` sets
``PRODUCER`` to a ProducerSpec (src/producers/contract.py), and only
declares; its work is a Work in work.py (src/producers/runner.py). Everything
that lists producers, their pools or their AI jobs reads them from here.

A plug-in the server admin installs brings its work while the API and the
scheduler run, so the list can change: ``use_added`` puts in place those added,
and ``registry()`` hands out a new dict each time it changes (never one
changed in place, so a caller holding one sees one list throughout). What's
worked out from the list is worked out again when it changes
(src/producers/live.py). An added producer is a plug-in's work and nothing
else (``_check_added``): it reads no original, makes nothing at a scan and
uses no AI job, so what's worked out from those once, at import (a reader's
work, the AI jobs), stays true.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Iterable

from src.producers.contract import AiJob, Pool, ProducerSpec
from src.producers.pools import SCANS

# An added producer's artifact starts with this; none of Lumiverb's own does.
ADDED_PREFIX = "plugin_"

_registry: dict[str, ProducerSpec] | None = None  # Lumiverb's own, found in this package
_added: dict[str, ProducerSpec] = {}  # added while running
# The list handed out, and what it was made from: made again when either changes.
_listed: tuple[dict[str, ProducerSpec], dict[str, ProducerSpec], dict[str, ProducerSpec]] | None = None
_adding = threading.Lock()


def _own() -> dict[str, ProducerSpec]:
    """Lumiverb's own producers by artifact, in list order. Found on first use."""
    global _registry
    if _registry is None:
        found: list[ProducerSpec] = []
        for module in pkgutil.iter_modules(__path__):
            if module.ispkg:
                spec = getattr(importlib.import_module(f"{__name__}.{module.name}"), "PRODUCER", None)
                if not isinstance(spec, ProducerSpec):
                    # Half-loaded (it imported what imports the registry) or not a producer:
                    # left out quietly, it would never run and nothing would say why.
                    raise ValueError(f"src/producers/{module.name} declares no PRODUCER (a ProducerSpec)")
                found.append(spec)
        found.sort(key=lambda p: (p.order, p.artifact))
        _check(found)
        _registry = {p.artifact: p for p in found}
    return _registry


def registry() -> dict[str, ProducerSpec]:
    """Every producer by its artifact, in list order: Lumiverb's own and those
    added. A new dict when that changes; never change the one handed out."""
    global _listed
    own, added = _own(), _added
    listed = _listed
    if listed is None or listed[0] is not own or listed[1] is not added:
        ordered = sorted((*own.values(), *added.values()), key=lambda p: (p.order, p.artifact))
        listed = _listed = (own, added, {p.artifact: p for p in ordered})
    return listed[2]


def use_added(specs: Iterable[ProducerSpec]) -> bool:
    """These producers added (each installed plug-in's work), in place of
    those added before: the list changes only when they differ. Each is
    checked with Lumiverb's own as they are at start (ValueError, and the list
    stays as it was). Whether it changed."""
    global _added
    with _adding:
        new = {p.artifact: p for p in specs}
        if new == _added:
            return False
        own = _own()
        for p in new.values():
            _check_added(p, own)
        _check([*own.values(), *new.values()], added=frozenset(new))
        _added = new
        return True


def _check_added(p: ProducerSpec, own: dict[str, ProducerSpec]) -> None:
    """An added producer is a plug-in's work, which the scheduler runs from
    what Lumiverb keeps: it reads no original, makes nothing at a scan, uses no
    AI job, can always be made again and is no part of another."""
    if not p.artifact.startswith(ADDED_PREFIX) or p.artifact in own:
        raise ValueError(f"{p.artifact}: an added producer's artifact starts with {ADDED_PREFIX!r}"
                         " and is none of Lumiverb's own")
    if not p.scheduled or p.storage or p.made_at_scan or p.job or p.cant_redo or p.part_of or p.regroup \
            or p.uniform or p.redo_also:
        raise ValueError(f"{p.artifact}: an added producer is run by the scheduler from what Lumiverb keeps"
                         " (no storage, made_at_scan, AI job, cant_redo, part_of, regroup, uniform or redo_also)")
    if p.pool is None or p.pool.name in pools_of(own.values()) or p.pool.sized_by or p.pool.gpu_hold:
        raise ValueError(f"{p.artifact}: an added producer's pool is its own, so many jobs at once"
                         " (none of Lumiverb's, no sized_by or gpu_hold)")


def _check(found: list[ProducerSpec], added: frozenset[str] = frozenset()) -> None:
    """What would otherwise go wrong quietly: two producers making one thing,
    one the scheduler runs missing what it needs to queue and run it, or two
    declaring one pool or AI job differently. added: those added while
    running (a plug-in's work), whose plug-in is an installed one."""
    from src.processing.machine import Machine

    for p in found:
        if p.artifact.startswith(ADDED_PREFIX) and p.artifact not in added:
            raise ValueError(f"{p.artifact}: {ADDED_PREFIX!r} starts only a plug-in's work's artifact")
    for field in ("artifact", "kind", "flag"):
        values = [getattr(p, field) for p in found if getattr(p, field)]
        if len(set(values)) != len(values):
            raise ValueError(f"Two producers share a {field}: {sorted(values)}")
    pools: dict[str, Pool] = {SCANS.name: SCANS}
    jobs: dict[str, AiJob] = {}
    job_pools: dict[str, str] = {}
    for p in found:
        if p.scheduled and not (p.flag and p.run and ":" in p.run and p.pool):
            raise ValueError(f"{p.artifact}: a producer the scheduler runs needs a flag, a run and a pool")
        if p.flag and not p.flag.startswith("missing_"):
            raise ValueError(f"{p.artifact}: its flag is a missing_* filter, not {p.flag!r}")
        if p.unit not in ("second", "clip"):
            raise ValueError(f"{p.artifact}: its unit is a second (of video) or a clip, not {p.unit!r}")
        levels = [s for s in p.settings if s.kind == "level"]
        if len(levels) > 1 or any(s.remakes or s.default not in s.choices for s in levels):
            raise ValueError(f"{p.artifact}: one level setting at most, kept out of the hash"
                             " (remakes=False), its default among its choices")
        others = {s.key for s in p.settings}
        for s in p.settings:
            if s.kind != "notch":
                continue
            stands_for = {tuple(sorted(m)) for m in s.means.values()}
            if not s.remakes or s.default not in s.choices or set(s.means) != set(s.choices) \
                    or len(stands_for) != 1 or set(next(iter(stands_for))) & others:
                raise ValueError(f"{p.artifact}: its notch {s.key!r} is in the hash (remakes=True), its default"
                                 " among its choices, each choice meaning the same keys, none another setting's")
        if p.plugin and p.artifact not in added:
            from src.plugins import plugin

            if plugin(p.plugin) is None:
                raise ValueError(f"{p.artifact}: it belongs to the plug-in {p.plugin!r}, which isn't one")
        if p.part_of and p.part_of not in {q.artifact for q in found}:
            raise ValueError(f"{p.artifact}: it's part of {p.part_of!r}, which no producer makes")
        missing = [n for n in (*p.needs, *p.redo_also) if n not in {q.artifact for q in found}]
        if missing:
            raise ValueError(f"{p.artifact} names what no producer makes: {missing}")
        if p.pool is None:
            continue
        if pools.setdefault(p.pool.name, p.pool) != p.pool:
            raise ValueError(f"{p.artifact}: the pool {p.pool.name!r} is declared another way elsewhere")
        if p.pool.sized_by and not hasattr(Machine(), p.pool.sized_by):
            raise ValueError(f"{p.artifact}: no setting of this machine's sizes a pool: {p.pool.sized_by!r}")
        if p.pool.job is not None:
            job = p.pool.job
            if jobs.setdefault(job.name, job) != job:
                raise ValueError(f"{p.artifact}: the AI job {job.name!r} is declared another way elsewhere")
            if job_pools.setdefault(job.name, p.pool.name) != p.pool.name:
                raise ValueError(f"{p.artifact}: the AI job {job.name!r} has one pool (its machines)")
            if p.setting("model") is None:
                raise ValueError(f"{p.artifact}: a producer an AI job makes takes its model (a setting \"model\")")


def pools() -> dict[str, Pool]:
    """Every pool by name: the scan pass's, then each producer's."""
    return pools_of(registry().values())


def pools_of(producers: Iterable[ProducerSpec]) -> dict[str, Pool]:
    out: dict[str, Pool] = {SCANS.name: SCANS}
    for p in producers:
        if p.pool is not None:
            out.setdefault(p.pool.name, p.pool)
    return out


def jobs() -> dict[str, AiJob]:
    """Every AI job the producers' machines do, by name (Settings → AI)."""
    return {p.pool.job.name: p.pool.job for p in registry().values() if p.pool is not None and p.pool.job}


def load(path: str):
    """What a "module:name" path names (a producer's work, a guard, a regroup)."""
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)
