"""The producers: one folder each, found here (ADR-016 phase 4).

Each subpackage of this one is a producer: its ``__init__`` sets
``PRODUCER`` to a ProducerSpec (src/producers/contract.py). Adding a
producer is adding a folder; everything that lists producers reads them
from registry().
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
                if isinstance(spec, ProducerSpec):
                    found.append(spec)
        found.sort(key=lambda p: (p.order, p.artifact))
        artifacts = [p.artifact for p in found]
        if len(set(artifacts)) != len(artifacts):
            raise ValueError(f"Two producers make one artifact: {sorted(artifacts)}")
        _registry = {p.artifact: p for p in found}
    return _registry


def load(path: str):
    """The function a producer's ``run`` names ("module:function")."""
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)
