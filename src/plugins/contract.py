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
    # Where it shows in lists.
    order: int = 1000
