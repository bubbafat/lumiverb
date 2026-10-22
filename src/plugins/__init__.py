"""Plug-ins (ADR-023): what Lumiverb reads of one, and an account's view of it.

Every plug-in is a program of its own with a manifest (manifest.py), Lumiverb's
own (src/own_plugins/) and those the server admin installs alike; an account
turns one on or off as its PluginSpec says (contract.py). ADR-021's plug-ins
in Lumiverb's code are gone since ADR-023 phase 3.
"""

from __future__ import annotations

import re

# An acknowledgment's version: numbers with dots, compared as numbers, in SQL too
# (src/server/repository/plugins.py on_sql).
_VERSION = re.compile(r"^[0-9]{1,6}(\.[0-9]{1,6}){0,3}$")


def version_key(version: str | None) -> tuple[int, ...] | None:
    """A version as numbers, to compare; None for one that isn't a version."""
    if version is None or not _VERSION.fullmatch(version):
        return None
    return tuple(int(part) for part in version.split("."))
