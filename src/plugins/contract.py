"""What a plug-in declares (ADR-021): an optional part of Lumiverb that an
account turns on or off, and what turning it on asks first, if anything.

A plug-in is one folder under src/plugins/, its __init__.py setting
PLUGIN = PluginSpec(...); src/plugins/__init__.py lists them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class Acknowledgment:
    """What an admin accepts before the plug-in is on in their account: a
    license, say. Shown in full; accepting records who, which version and
    when. A new version (its text changed in a release) turns the plug-in
    off in every account until an admin accepts it."""

    version: str
    title: str
    # Paragraphs, separated by a blank line.
    text: str
    links: tuple[Link, ...] = ()


@dataclass(frozen=True)
class PluginSpec:
    plugin_id: str  # lowercase letters, digits and dashes: in URLs and the CLI
    title: str
    # What it does, a sentence or two (Settings › Plug-ins).
    description: str
    acknowledgment: Acknowledgment | None = None
    # What it keeps for the whole server, fetched once when an account turns it
    # on (src/server/plugin_data.py): "module:function", called with a
    # progress(done, total, detail) and returning how many things it keeps.
    data: str = ""
    # What that is and what it counts, for people: "The place list", "places".
    data_title: str = ""
    data_unit: str = ""
    # Where it shows in lists.
    order: int = 1000
