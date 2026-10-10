"""The producers: one folder each, found here (ADR-016 phase 4).

Each subpackage of this one is a producer: its ``__init__`` sets
``PRODUCER`` to a ProducerSpec (src/producers/contract.py), and only
declares; its work is a Work in work.py (src/producers/runner.py). Everything
that lists producers, their pools or their AI jobs reads them from here.
"""

from __future__ import annotations

import importlib
import pkgutil

from src.producers.contract import AiJob, Pool, ProducerSpec
from src.producers.pools import SCANS

_registry: dict[str, ProducerSpec] | None = None


def registry() -> dict[str, ProducerSpec]:
    """Every producer by its artifact, in list order. Found on first use."""
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


def _check(found: list[ProducerSpec]) -> None:
    """What would otherwise go wrong quietly: two producers making one thing,
    one the scheduler runs missing what it needs to queue and run it, or two
    declaring one pool or AI job differently."""
    from src.processing.machine import Machine

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
    out: dict[str, Pool] = {SCANS.name: SCANS}
    for p in registry().values():
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
