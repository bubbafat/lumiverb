"""Filter registry — auto-discovers leaf filter types and provides parsing + capabilities.

All concrete LeafFilter subclasses from query_filter.py are registered automatically.
The registry provides:
  - parse_f_params(): URL ?f= params → QuerySpec
  - from_json(): a search as JSON (a project's from_search) → QuerySpec
  - capabilities(): list of filter type descriptors for GET /v1/filters/capabilities
"""

from __future__ import annotations

import logging

from src.server.models.query_filter import (
    ApertureRange,
    CameraMake,
    CameraModel,
    ColorLabel,
    Combinator,
    DateRange,
    ExposureRange,
    Favorite,
    FocalLengthRange,
    GroupFilter,
    HasColor,
    HasExposure,
    HasFaces,
    HasGps,
    HasRating,
    IsoRange,
    LeafFilter,
    LensModel,
    LibraryScope,
    MediaType,
    NearLocation,
    PathPrefix,
    PersonFilter,
    QuerySpec,
    SearchTerm,
    StarRange,
    TagFilter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Registry — maps prefix → class and type_name → class
# ---------------------------------------------------------------------------

_ALL_FILTER_TYPES: list[type[LeafFilter]] = [
    SearchTerm,
    LibraryScope,
    PathPrefix,
    MediaType,
    CameraMake,
    CameraModel,
    LensModel,
    IsoRange,
    ApertureRange,
    FocalLengthRange,
    ExposureRange,
    HasExposure,
    HasGps,
    NearLocation,
    DateRange,
    Favorite,
    StarRange,
    ColorLabel,
    HasRating,
    HasColor,
    HasFaces,
    PersonFilter,
    TagFilter,
]

PREFIX_MAP: dict[str, type[LeafFilter]] = {cls.prefix(): cls for cls in _ALL_FILTER_TYPES}
TYPE_MAP: dict[str, type[LeafFilter]] = {cls.type_name(): cls for cls in _ALL_FILTER_TYPES}


# ---------------------------------------------------------------------------
# Parsing: URL ?f= params → QuerySpec
# ---------------------------------------------------------------------------

def parse_f_params(
    f_params: list[str],
    sort: str = "taken_at",
    direction: str = "desc",
) -> QuerySpec:
    """Parse repeated ?f=prefix:value URL params into a QuerySpec.

    Unknown prefixes are silently ignored (forward compatibility).
    """
    leaves: list[LeafFilter] = []
    for raw in f_params:
        colon = raw.find(":")
        if colon <= 0:
            # No colon or starts with colon — treat as bare search term
            if raw.strip():
                leaves.append(SearchTerm(q=raw.strip()))
            continue
        prefix = raw[:colon]
        value = raw[colon + 1:]
        cls = PREFIX_MAP.get(prefix)
        if cls is None:
            logger.debug("Unknown filter prefix %r, ignoring", prefix)
            continue
        try:
            leaves.append(cls.from_url_value(value))
        except (ValueError, IndexError) as exc:
            logger.warning("Failed to parse filter %r: %s", raw, exc)
            continue

    return QuerySpec(
        root=GroupFilter(combinator=Combinator.AND, children=tuple(leaves)),
        sort=sort,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Deserialization: a search as JSON → QuerySpec
# ---------------------------------------------------------------------------

def from_json(data: object) -> QuerySpec:
    """A search as JSON (a project's from_search) → QuerySpec.

    Expected format:
    {
        "filters": [
            {"type": "camera_make", "value": "Canon"},
            {"type": "query", "value": "Disney"},
            ...
        ],
        "sort": "taken_at",       # optional
        "direction": "desc"       # optional
    }

    Strict: a filter it can't read raises ValueError, saying which. Dropping
    it instead would widen the search, and a project made from it would hold
    clips nobody asked for (a typo'd "camera" for "camera_make": the whole
    library).
    """
    if not isinstance(data, dict):
        raise ValueError("A search is an object with a filters list")
    raw_filters = data.get("filters", [])
    if not isinstance(raw_filters, list):
        raise ValueError("filters must be a list")

    leaves: list[LeafFilter] = []
    for item in raw_filters:
        if not isinstance(item, dict):
            raise ValueError(f"A filter is an object with a type and a value, not {item!r}")
        type_name = item.get("type", "")
        cls = TYPE_MAP.get(type_name) if isinstance(type_name, str) else None
        if cls is None:
            raise ValueError(f"Unknown filter type {type_name!r}")
        try:
            leaves.append(cls.from_json(item))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"Can't read the {type_name} filter {item!r}: {exc}") from None

    sort = data.get("sort", "taken_at")
    direction = data.get("direction", "desc")
    if not isinstance(sort, str):
        raise ValueError("sort must be a column name")
    if direction not in ("asc", "desc"):
        raise ValueError('direction must be "asc" or "desc"')
    return QuerySpec(
        root=GroupFilter(combinator=Combinator.AND, children=tuple(leaves)),
        sort=sort,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Capabilities: for GET /v1/filters/capabilities
# ---------------------------------------------------------------------------

def capabilities() -> list[dict]:
    """Return the filter capabilities catalog for the capabilities endpoint."""
    result = []
    for cls in _ALL_FILTER_TYPES:
        entry: dict = {
            "prefix": cls.prefix(),
            "label": cls.display_label(),
            "value_kind": cls.value_kind().value,
        }
        if cls.faceted():
            entry["faceted"] = True
        ev = cls.enum_values()
        if ev is not None:
            entry["enum_values"] = ev
        result.append(entry)
    return result
