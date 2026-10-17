"""Who reads a library's files (ADR-018 decision 1): this server, or a
registered device (its id, `dev_…`), never both at once.

The reader is the one that scans the library, looks at its storage, makes
what needs its originals (probing, capture, analysis proxies) and is where
its folder is chosen. Every caller that does any of that asks
read_by_server, so a library a device reads is left alone by this server
everywhere at once. What's made from its previews and analysis proxies
(search, faces, descriptions, scenes, transcripts) is this server's either
way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SERVER = "server"


def read_by_server(library: Mapping[str, Any] | Any) -> bool:
    """Whether this server reads the library's files: a row, a model or an
    API answer (a dict). A library that names no reader (an answer from
    before readers) is this server's, as every library was then."""
    reader = library.get("reader") if isinstance(library, Mapping) else getattr(library, "reader", None)
    return (reader or SERVER) == SERVER
