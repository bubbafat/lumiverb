"""The producers: one folder each, found here (ADR-016 phase 4).

Each subpackage of this one is a producer: its ``__init__`` sets
``PRODUCER`` to a ProducerSpec (src/producers/contract.py), and only
declares. The scheduler, the reconciler, the lineage and GET /v1/producers
read them from registry(); what's still named by hand is listed in
contract.py.
"""

from __future__ import annotations

import importlib
import pkgutil

from src.producers.contract import ProducerSpec

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
    or one the scheduler runs missing what it needs to queue and run it."""
    for field in ("artifact", "kind", "flag"):
        values = [getattr(p, field) for p in found if getattr(p, field)]
        if len(set(values)) != len(values):
            raise ValueError(f"Two producers share a {field}: {sorted(values)}")
    for p in found:
        if p.scheduled and not (p.flag and p.run and ":" in p.run and p.pool):
            raise ValueError(f"{p.artifact}: a producer the scheduler runs needs a flag, a run and a pool")
        if p.unit not in ("second", "clip"):
            raise ValueError(f"{p.artifact}: its unit is a second (of video) or a clip, not {p.unit!r}")
        if p.per_account and not (p.job and p.pool == p.job):
            raise ValueError(f"{p.artifact}: an account's pool is its AI job's machines (pool = job)")
        missing = [n for n in (*p.needs, *p.redo_also) if n not in {q.artifact for q in found}]
        if missing:
            raise ValueError(f"{p.artifact} names what no producer makes: {missing}")


def load(path: str):
    """The function a producer's ``run`` names ("module:function")."""
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)
