"""The plug-ins: one folder each, found here (ADR-021).

Each subpackage of this one is a plug-in: its ``__init__`` sets ``PLUGIN``
to a PluginSpec (src/plugins/contract.py). Everything that lists plug-ins
reads them from here.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

from src.plugins.contract import PluginSpec

_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
# An acknowledgment's version: in SQL (src/server/repository/plugins.py on_sql), so plain.
_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_registry: dict[str, PluginSpec] | None = None


def registry() -> dict[str, PluginSpec]:
    """Every plug-in by its id, in list order. Found on first use."""
    global _registry
    if _registry is None:
        found: list[PluginSpec] = []
        for module in pkgutil.iter_modules(__path__):
            if module.ispkg:
                spec = getattr(importlib.import_module(f"{__name__}.{module.name}"), "PLUGIN", None)
                if not isinstance(spec, PluginSpec):
                    raise ValueError(f"src/plugins/{module.name} declares no PLUGIN (a PluginSpec)")
                found.append(spec)
        found.sort(key=lambda p: (p.order, p.plugin_id))
        ids = [p.plugin_id for p in found]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Two plug-ins share an id: {sorted(ids)}")
        for p in found:
            if not _ID.match(p.plugin_id):
                raise ValueError(f"{p.plugin_id!r}: a plug-in's id is lowercase letters, digits and dashes")
            if p.acknowledgment is not None and not (_VERSION.match(p.acknowledgment.version)
                                                     and p.acknowledgment.text.strip()):
                raise ValueError(f"{p.plugin_id}: an acknowledgment needs a version (letters, digits, . _ -) "
                                 "and its text")
        _registry = {p.plugin_id: p for p in found}
    return _registry


def plugin(plugin_id: str) -> PluginSpec | None:
    return registry().get(plugin_id)
