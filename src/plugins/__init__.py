"""The plug-ins: one folder each, found here (ADR-021).

Each subpackage of this one is a plug-in: its ``__init__`` sets ``PLUGIN``
to a PluginSpec (src/plugins/contract.py), or to None for one that's no
longer one of these (GeoNames, an installed plug-in since ADR-023 phase 3:
src/own_plugins/geonames/; its old list's code stays for part 3's
fallbacks until part 4). Everything that lists plug-ins reads them from
here.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

from src.plugins.contract import PluginSpec

_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
# An acknowledgment's version: numbers with dots, compared as numbers, in SQL too
# (src/server/repository/plugins.py on_sql).
_VERSION = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,6}){0,3}$")


def version_key(version: str | None) -> tuple[int, ...] | None:
    """A version as numbers, to compare; None for one that isn't a version."""
    if version is None or not _VERSION.match(version):
        return None
    return tuple(int(part) for part in version.split("."))
_registry: dict[str, PluginSpec] | None = None


def registry() -> dict[str, PluginSpec]:
    """Every plug-in by its id, in list order. Found on first use."""
    global _registry
    if _registry is None:
        found: list[PluginSpec] = []
        for module in pkgutil.iter_modules(__path__):
            if module.ispkg:
                package = importlib.import_module(f"{__name__}.{module.name}")
                spec = getattr(package, "PLUGIN", None)
                if spec is None and hasattr(package, "PLUGIN"):
                    continue  # no longer one of these
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
            ack = p.acknowledgment
            if ack is not None and not (version_key(ack.version) and ack.text.strip()):
                raise ValueError(f"{p.plugin_id}: an acknowledgment needs a version (numbers with dots: "
                                 "\"1\", \"2.1\") and its text")
            if ack is not None and ack.reaccept_from and not (
                    version_key(ack.reaccept_from) and version_key(ack.reaccept_from) <= version_key(ack.version)):
                raise ValueError(f"{p.plugin_id}: reaccept_from is a version up to its own ({ack.version})")
        _registry = {p.plugin_id: p for p in found}
    return _registry


def plugin(plugin_id: str) -> PluginSpec | None:
    return registry().get(plugin_id)
