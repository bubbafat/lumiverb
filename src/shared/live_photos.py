"""A Live Photo's identifier: Apple's content identifier, the same in the photo (its maker notes) and in the
few seconds of video beside it (the .mov's QuickTime keys). What a scan sends and the server keeps
(assets.live_photo_id, src/server/repository/live_photos.py)."""

from __future__ import annotations

import re

# As Apple writes it, a UUID; any letters, digits and dashes up to 64 are taken, uppercase.
_ID = re.compile(r"^[0-9A-Z-]{1,64}$")
PATTERN = _ID.pattern

# A clip's name without its extension, lowercase: ix_assets_stem's expression (tenant migration b5d1f7a3c9e2),
# so a query written with it uses the index.
STEM = r"lower(regexp_replace({a}.rel_path, '\.[^./]*$', ''))"


def stem(alias: str) -> str:
    """SQL: the clip `alias`'s name without its extension (STEM)."""
    return STEM.format(a=alias)


def identifier(raw: object) -> str | None:
    """The identifier as kept, uppercase; None for anything that isn't one."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().upper()
    return value if _ID.match(value) else None
