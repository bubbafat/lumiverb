"""Content hashes as the API takes them (src/shared/content_hash.py).

A content hash names its scheme ("scheme:value"); the server takes only
its own schemes, exactly as it makes them, and refuses a bare hash rather
than guess how it was made (docs/cursor-api.md, "Content hashes").
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from src.shared import content_hash

TAKEN = ("sha256:<64 lowercase hex digits> (the whole file's SHA-256: a file up to 64 MiB)"
         " or fp1:<64 lowercase hex digits> (a bigger file's)")


def why_not(value: object) -> str | None:
    """Why value isn't a content hash the server takes, or None when it is one."""
    if isinstance(value, str) and content_hash.ours(value):
        return None
    if isinstance(value, str) and not content_hash.well_formed(value):
        return f"a content hash is scheme:value, its scheme named; this server takes {TAKEN}"
    return f"this server takes content hashes {TAKEN}"


def _checked(value: str) -> str:
    if (why := why_not(value)) is not None:
        raise ValueError(why)
    return value


# A request field holding a content hash.
ContentHash = Annotated[str, AfterValidator(_checked)]
