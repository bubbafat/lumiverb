"""Filter registry — auto-discovers leaf filter types and provides parsing + capabilities.

All concrete LeafFilter subclasses from query_filter.py are registered automatically.
The registry provides:
  - parse_f_params(): URL ?f= params → QuerySpec
  - from_json(): saved_query JSON → QuerySpec
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
# Deserialization: saved_query JSON → QuerySpec
# ---------------------------------------------------------------------------

def from_json(data: dict) -> QuerySpec:
    """Deserialize a smart collection saved_query JSON into a QuerySpec.

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
    """
    leaves: list[LeafFilter] = []
    for item in data.get("filters", []):
        type_name = item.get("type", "")
        cls = TYPE_MAP.get(type_name)
        if cls is None:
            logger.warning("Unknown filter type %r in saved_query, ignoring", type_name)
            continue
        try:
            leaves.append(cls.from_json(item))
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to deserialize filter %r: %s", item, exc)
            continue

    return QuerySpec(
        root=GroupFilter(combinator=Combinator.AND, children=tuple(leaves)),
        sort=data.get("sort", "taken_at"),
        direction=data.get("direction", "desc"),
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
