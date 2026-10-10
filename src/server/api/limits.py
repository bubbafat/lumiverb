"""Bounds every route shares, and the rule for what an action acts on.

Past a bound is a 422 (`Query(le=...)`, `Field(max_length=...)`), never a
silent clamp. An impactful action says what it acts on: its ids, or
`all: true`; neither (or both) is a 400 scope_required, never "everything".
"""

from __future__ import annotations

from src.server.api.errors import ApiError

# Ids in one request body (clips, faces, projects, libraries, views).
MAX_IDS = 10_000
# Items in one page of clips (/page, /query, a project's or a public page's clips, trash, archive).
MAX_PAGE = 500
# A base64 image sent to search by (about 8 MB decoded).
MAX_IMAGE_B64 = 11_200_000
# Results of one similarity search, and how deep it pages.
MAX_SIMILAR = 100
MAX_SIMILAR_OFFSET = 10_000


def require_scope(named: bool, everything: bool, ids: str) -> None:
    """400 scope_required unless the request names its targets (ids) or says all: true, not both."""
    if named == everything:
        raise ApiError(400, "scope_required", f"Give {ids}, or all: true." if not named else
                       f"Give {ids} or all: true, not both.")
