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
    license, say. Shown in full at every turn-on; accepting records who,
    which version and when. A new version (its text changed in a release)
    is accepted again only when the plug-in says so (reaccept_from, Robert
    2026-10-18): otherwise what an account accepted before still stands."""

    # A number, or numbers with dots ("1", "2", "2.1"), compared as numbers.
    version: str
    title: str
    # Paragraphs, separated by a blank line.
    text: str
    links: tuple[Link, ...] = ()
    # The earliest version whose acceptance still stands. A version that must be accepted again (its terms
    # changed: another license, say) names itself here; one that needn't (its wording, a bigger download
    # under the same license) keeps the one before's, so a server that skipped a release that asked still
    # asks. Empty: any version accepted stands.
    reaccept_from: str = ""


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
