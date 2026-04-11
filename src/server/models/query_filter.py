"""QueryFilter algebra — composable, self-describing filter tree.

The filter tree is a recursive structure:
  - LeafFilter: one predicate (e.g., CameraMake("Canon"), StarRange(min=3))
  - GroupFilter: AND or OR over child filters
  - QuerySpec: top-level wrapper with sort + direction alongside the tree

Each LeafFilter subclass is self-contained: it knows its URL prefix, display label,
SQL generation, JSON serialization, and whether it needs special JOINs. Adding a new
filter type means writing one class — no changes to generic infrastructure.

Clients never need to know filter internals. The /v1/filters/capabilities endpoint
returns the list of known filter types with their prefix and value_kind. Clients render
generically per value_kind (text, string, boolean, enum, int_range, float_range,
date_range, location).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import ClassVar


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ValueKind(str, Enum):
    TEXT = "text"
    STRING = "string"
    BOOLEAN = "boolean"
    ENUM = "enum"
    INT_RANGE = "int_range"
    FLOAT_RANGE = "float_range"
    DATE_RANGE = "date_range"
    LOCATION = "location"


class Combinator(str, Enum):
    AND = "and"
    OR = "or"


# ---------------------------------------------------------------------------
# Base types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeafFilter(ABC):
    """Abstract base for all filter predicates."""

    @classmethod
    @abstractmethod
    def prefix(cls) -> str:
        """URL query prefix, e.g. 'camera_make'."""
        ...

    @classmethod
    @abstractmethod
    def type_name(cls) -> str:
        """JSON type discriminator. Usually same as prefix."""
        ...

    @classmethod
    @abstractmethod
    def value_kind(cls) -> ValueKind:
        ...

    @classmethod
    def display_label(cls) -> str:
        """Human-readable label for capabilities endpoint."""
        return cls.prefix().replace("_", " ").title()

    @classmethod
    def faceted(cls) -> bool:
        """Whether the facets endpoint can enumerate values for this filter."""
        return False

    @classmethod
    def enum_values(cls) -> list[str] | None:
        """For ENUM value_kind, the valid options."""
        return None

    @abstractmethod
    def label(self) -> str:
        """Chiclet display label for this specific filter instance."""
        ...

    @abstractmethod
    def to_sql(self, params: dict, counter: list[int]) -> str:
        """Return SQL WHERE fragment, adding bind params to `params`.

        Use counter[0] for unique param names, incrementing after each use.
        """
        ...

    @abstractmethod
    def to_url_value(self) -> str:
        """Serialize value for URL: used in f=prefix:VALUE."""
        ...

    @classmethod
    @abstractmethod
    def from_url_value(cls, raw: str) -> LeafFilter:
        """Parse from the VALUE portion of f=prefix:VALUE."""
        ...

    def to_json(self) -> dict:
        """Serialize for smart collection saved_query."""
        return {"type": self.type_name(), "value": self.to_url_value()}

    @classmethod
    def from_json(cls, data: dict) -> LeafFilter:
        """Deserialize from saved_query JSON."""
        return cls.from_url_value(data["value"])

    @property
    def needs_rating_join(self) -> bool:
        return False

    @property
    def needs_metadata_join(self) -> bool:
        return False

    def to_quickwit(self) -> str | None:
        """Return Quickwit query clause, or None if not a text search filter."""
        return None

    def _param_name(self, base: str, counter: list[int]) -> str:
        """Generate a unique bind parameter name."""
        name = f"{base}_{counter[0]}"
        counter[0] += 1
        return name


@dataclass(frozen=True)
class GroupFilter:
    """AND or OR group of child filters."""
    combinator: Combinator = Combinator.AND
    children: tuple[LeafFilter | GroupFilter, ...] = ()

    def to_json(self) -> dict:
        return {
            "op": self.combinator.value,
            "children": [c.to_json() for c in self.children],
        }


@dataclass
class QuerySpec:
    """Top-level query: a filter tree plus sort and direction."""
    root: GroupFilter = field(default_factory=GroupFilter)
    sort: str = "taken_at"
    direction: str = "desc"

    @property
    def leaves(self) -> list[LeafFilter]:
        """Flatten all leaves from the tree (works for any nesting depth)."""
        result: list[LeafFilter] = []
        _collect_leaves(self.root, result)
        return result

    @property
    def search_terms(self) -> list[SearchTerm]:
        return [f for f in self.leaves if isinstance(f, SearchTerm)]

    @property
    def structured_filters(self) -> list[LeafFilter]:
        return [f for f in self.leaves if not isinstance(f, SearchTerm)]

    @property
    def needs_rating_join(self) -> bool:
        return any(f.needs_rating_join for f in self.leaves)

    @property
    def needs_metadata_join(self) -> bool:
        return any(f.needs_metadata_join for f in self.leaves)

    def to_json(self) -> dict:
        """Serialize for smart collection saved_query."""
        children_json = [c.to_json() for c in self.root.children]
        result: dict = {"filters": children_json}
        if self.sort != "taken_at":
            result["sort"] = self.sort
        if self.direction != "desc":
            result["direction"] = self.direction
        return result


def _collect_leaves(node: LeafFilter | GroupFilter, out: list[LeafFilter]) -> None:
    if isinstance(node, LeafFilter):
        out.append(node)
    elif isinstance(node, GroupFilter):
        for child in node.children:
            _collect_leaves(child, out)


# ---------------------------------------------------------------------------
# Concrete leaf filters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchTerm(LeafFilter):
    q: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "query"

    @classmethod
    def type_name(cls) -> str:
        return "query"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.TEXT

    @classmethod
    def display_label(cls) -> str:
        return "Search"

    def label(self) -> str:
        return f'"{self.q}"'

    def to_sql(self, params: dict, counter: list[int]) -> str:
        # SearchTerm is handled by Quickwit, not SQL.
        # When used in SQL (postgres fallback), generate ILIKE conditions.
        p = self._param_name("q", counter)
        params[p] = f"%{self.q}%"
        return (
            f"(a.rel_path ILIKE :{p}"
            f" OR a.camera_make ILIKE :{p}"
            f" OR a.camera_model ILIKE :{p}"
            f" OR a.note ILIKE :{p}"
            f" OR a.transcript_text ILIKE :{p})"
        )

    def to_url_value(self) -> str:
        return self.q

    @classmethod
    def from_url_value(cls, raw: str) -> SearchTerm:
        return cls(q=raw)

    def to_quickwit(self) -> str | None:
        return self.q


@dataclass(frozen=True)
class LibraryScope(LeafFilter):
    library_ids: tuple[str, ...] = ()

    @classmethod
    def prefix(cls) -> str:
        return "library"

    @classmethod
    def type_name(cls) -> str:
        return "library"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    def label(self) -> str:
        if len(self.library_ids) == 1:
            return f"Library"
        return f"{len(self.library_ids)} libraries"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("library_ids", counter)
        params[p] = list(self.library_ids)
        return f"a.library_id = ANY(:{p})"

    def to_url_value(self) -> str:
        return ",".join(self.library_ids)

    @classmethod
    def from_url_value(cls, raw: str) -> LibraryScope:
        ids = tuple(v.strip() for v in raw.split(",") if v.strip())
        return cls(library_ids=ids)


@dataclass(frozen=True)
class PathPrefix(LeafFilter):
    path: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "path"

    @classmethod
    def type_name(cls) -> str:
        return "path"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    def label(self) -> str:
        return f"/{self.path}"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p_exact = self._param_name("path_prefix", counter)
        p_like = self._param_name("path_prefix_like", counter)
        params[p_exact] = self.path
        params[p_like] = self.path + "/%"
        return f"(a.rel_path = :{p_exact} OR a.rel_path LIKE :{p_like})"

    def to_url_value(self) -> str:
        return self.path

    @classmethod
    def from_url_value(cls, raw: str) -> PathPrefix:
        # Security: reject path traversal
        if ".." in raw:
            raise ValueError("Path prefix must not contain '..'")
        return cls(path=raw)


@dataclass(frozen=True)
class MediaType(LeafFilter):
    types: tuple[str, ...] = ()

    _VALID: ClassVar[set[str]] = {"image", "video"}

    @classmethod
    def prefix(cls) -> str:
        return "media"

    @classmethod
    def type_name(cls) -> str:
        return "media"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.ENUM

    @classmethod
    def enum_values(cls) -> list[str] | None:
        return ["image", "video"]

    def label(self) -> str:
        if self.types == ("image",):
            return "Photos only"
        if self.types == ("video",):
            return "Videos only"
        return ", ".join(self.types)

    def to_sql(self, params: dict, counter: list[int]) -> str:
        clauses = []
        for t in self.types:
            if t in self._VALID:
                p = self._param_name("media_type", counter)
                params[p] = t
                clauses.append(f"a.media_type = :{p}")
        if not clauses:
            return "TRUE"
        return f"({' OR '.join(clauses)})"

    def to_url_value(self) -> str:
        return ",".join(self.types)

    @classmethod
    def from_url_value(cls, raw: str) -> MediaType:
        types = tuple(v.strip() for v in raw.split(",") if v.strip())
        return cls(types=types)


@dataclass(frozen=True)
class CameraMake(LeafFilter):
    value: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "camera_make"

    @classmethod
    def type_name(cls) -> str:
        return "camera_make"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    @classmethod
    def faceted(cls) -> bool:
        return True

    def label(self) -> str:
        return self.value

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("camera_make", counter)
        params[p] = self.value
        return f"a.camera_make = :{p}"

    def to_url_value(self) -> str:
        return self.value

    @classmethod
    def from_url_value(cls, raw: str) -> CameraMake:
        return cls(value=raw)


@dataclass(frozen=True)
class CameraModel(LeafFilter):
    value: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "camera_model"

    @classmethod
    def type_name(cls) -> str:
        return "camera_model"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    @classmethod
    def faceted(cls) -> bool:
        return True

    def label(self) -> str:
        return self.value

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("camera_model", counter)
        params[p] = self.value
        return f"a.camera_model = :{p}"

    def to_url_value(self) -> str:
        return self.value

    @classmethod
    def from_url_value(cls, raw: str) -> CameraModel:
        return cls(value=raw)


@dataclass(frozen=True)
class LensModel(LeafFilter):
    value: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "lens"

    @classmethod
    def type_name(cls) -> str:
        return "lens"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    @classmethod
    def faceted(cls) -> bool:
        return True

    def label(self) -> str:
        return self.value

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("lens_model", counter)
        params[p] = self.value
        return f"a.lens_model = :{p}"

    def to_url_value(self) -> str:
        return self.value

    @classmethod
    def from_url_value(cls, raw: str) -> LensModel:
        return cls(value=raw)


def _parse_range(raw: str) -> tuple[str | None, str | None]:
    """Parse 'min-max', 'min+', '-max', or 'exact' range strings."""
    if raw.endswith("+"):
        return raw[:-1], None
    if raw.startswith("-"):
        return None, raw[1:]
    if "-" in raw:
        parts = raw.split("-", 1)
        return parts[0], parts[1]
    # Single value = exact match (min == max)
    return raw, raw


@dataclass(frozen=True)
class IsoRange(LeafFilter):
    min_val: int | None = None
    max_val: int | None = None

    @classmethod
    def prefix(cls) -> str:
        return "iso"

    @classmethod
    def type_name(cls) -> str:
        return "iso"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.INT_RANGE

    def label(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return f"ISO {self.min_val}"
            return f"ISO {self.min_val}\u2013{self.max_val}"
        if self.min_val is not None:
            return f"ISO {self.min_val}+"
        return f"ISO \u2264{self.max_val}"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.min_val is not None:
            p = self._param_name("iso_min", counter)
            params[p] = self.min_val
            parts.append(f"a.iso >= :{p}")
        if self.max_val is not None:
            p = self._param_name("iso_max", counter)
            params[p] = self.max_val
            parts.append(f"a.iso <= :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return str(self.min_val)
            return f"{self.min_val}-{self.max_val}"
        if self.min_val is not None:
            return f"{self.min_val}+"
        return f"-{self.max_val}"

    @classmethod
    def from_url_value(cls, raw: str) -> IsoRange:
        lo, hi = _parse_range(raw)
        return cls(
            min_val=int(lo) if lo else None,
            max_val=int(hi) if hi else None,
        )


@dataclass(frozen=True)
class ApertureRange(LeafFilter):
    min_val: float | None = None
    max_val: float | None = None

    @classmethod
    def prefix(cls) -> str:
        return "aperture"

    @classmethod
    def type_name(cls) -> str:
        return "aperture"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.FLOAT_RANGE

    def label(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return f"f/{self.min_val}"
            return f"f/{self.min_val}\u2013{self.max_val}"
        if self.min_val is not None:
            return f"f/{self.min_val}+"
        return f"f/\u2264{self.max_val}"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.min_val is not None:
            p = self._param_name("aperture_min", counter)
            params[p] = self.min_val
            parts.append(f"a.aperture >= :{p}")
        if self.max_val is not None:
            p = self._param_name("aperture_max", counter)
            params[p] = self.max_val
            parts.append(f"a.aperture <= :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return str(self.min_val)
            return f"{self.min_val}-{self.max_val}"
        if self.min_val is not None:
            return f"{self.min_val}+"
        return f"-{self.max_val}"

    @classmethod
    def from_url_value(cls, raw: str) -> ApertureRange:
        lo, hi = _parse_range(raw)
        return cls(
            min_val=float(lo) if lo else None,
            max_val=float(hi) if hi else None,
        )


@dataclass(frozen=True)
class FocalLengthRange(LeafFilter):
    min_val: float | None = None
    max_val: float | None = None

    @classmethod
    def prefix(cls) -> str:
        return "focal_length"

    @classmethod
    def type_name(cls) -> str:
        return "focal_length"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.FLOAT_RANGE

    def label(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return f"{self.min_val}mm"
            return f"{self.min_val}\u2013{self.max_val}mm"
        if self.min_val is not None:
            return f"{self.min_val}mm+"
        return f"\u2264{self.max_val}mm"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.min_val is not None:
            p = self._param_name("focal_min", counter)
            params[p] = self.min_val
            parts.append(f"a.focal_length >= :{p}")
        if self.max_val is not None:
            p = self._param_name("focal_max", counter)
            params[p] = self.max_val
            parts.append(f"a.focal_length <= :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return str(self.min_val)
            return f"{self.min_val}-{self.max_val}"
        if self.min_val is not None:
            return f"{self.min_val}+"
        return f"-{self.max_val}"

    @classmethod
    def from_url_value(cls, raw: str) -> FocalLengthRange:
        lo, hi = _parse_range(raw)
        return cls(
            min_val=float(lo) if lo else None,
            max_val=float(hi) if hi else None,
        )


@dataclass(frozen=True)
class ExposureRange(LeafFilter):
    min_us: int | None = None
    max_us: int | None = None

    @classmethod
    def prefix(cls) -> str:
        return "exposure"

    @classmethod
    def type_name(cls) -> str:
        return "exposure"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.INT_RANGE

    @classmethod
    def display_label(cls) -> str:
        return "Exposure"

    def label(self) -> str:
        def _fmt(us: int) -> str:
            if us >= 1_000_000:
                return f"{us / 1_000_000:.1f}s"
            return f"1/{1_000_000 // us}"

        if self.min_us is not None and self.max_us is not None:
            return f"{_fmt(self.min_us)}\u2013{_fmt(self.max_us)}"
        if self.min_us is not None:
            return f"{_fmt(self.min_us)}+"
        return f"\u2264{_fmt(self.max_us)}"  # type: ignore[arg-type]

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.min_us is not None:
            p = self._param_name("exposure_min", counter)
            params[p] = self.min_us
            parts.append(f"a.exposure_time_us >= :{p}")
        if self.max_us is not None:
            p = self._param_name("exposure_max", counter)
            params[p] = self.max_us
            parts.append(f"a.exposure_time_us <= :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        if self.min_us is not None and self.max_us is not None:
            return f"{self.min_us}-{self.max_us}"
        if self.min_us is not None:
            return f"{self.min_us}+"
        return f"-{self.max_us}"

    @classmethod
    def from_url_value(cls, raw: str) -> ExposureRange:
        lo, hi = _parse_range(raw)
        return cls(
            min_us=int(lo) if lo else None,
            max_us=int(hi) if hi else None,
        )


def _bool_sql(column_expr: str, value: bool) -> str:
    """Helper for boolean IS [NOT] NULL style filters."""
    if value:
        return f"{column_expr} IS NOT NULL"
    return f"{column_expr} IS NULL"


@dataclass(frozen=True)
class HasExposure(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "has_exposure"

    @classmethod
    def type_name(cls) -> str:
        return "has_exposure"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Has exposure data" if self.value else "No exposure data"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "(a.iso IS NOT NULL OR a.exposure_time_us IS NOT NULL OR a.aperture IS NOT NULL)"
        return "a.iso IS NULL AND a.exposure_time_us IS NULL AND a.aperture IS NULL"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> HasExposure:
        return cls(value=raw.lower() in ("yes", "true", "1"))


@dataclass(frozen=True)
class HasGps(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "has_gps"

    @classmethod
    def type_name(cls) -> str:
        return "has_gps"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Has location" if self.value else "No location"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "a.gps_lat IS NOT NULL AND a.gps_lon IS NOT NULL"
        return "(a.gps_lat IS NULL OR a.gps_lon IS NULL)"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> HasGps:
        return cls(value=raw.lower() in ("yes", "true", "1"))


@dataclass(frozen=True)
class NearLocation(LeafFilter):
    lat: float = 0.0
    lon: float = 0.0
    radius_km: float = 1.0

    @classmethod
    def prefix(cls) -> str:
        return "near"

    @classmethod
    def type_name(cls) -> str:
        return "near"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.LOCATION

    def label(self) -> str:
        return f"Within {self.radius_km}km"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        lat_delta = self.radius_km / 111.0
        # Clamp latitude for cos() — avoid division by zero at poles
        clamped_lat = max(-89.9, min(89.9, self.lat))
        lon_delta = self.radius_km / (111.0 * math.cos(math.radians(clamped_lat)))

        p_min_lat = self._param_name("min_lat", counter)
        p_max_lat = self._param_name("max_lat", counter)
        p_min_lon = self._param_name("min_lon", counter)
        p_max_lon = self._param_name("max_lon", counter)

        params[p_min_lat] = self.lat - lat_delta
        params[p_max_lat] = self.lat + lat_delta
        params[p_min_lon] = self.lon - lon_delta
        params[p_max_lon] = self.lon + lon_delta

        return (
            f"a.gps_lat BETWEEN :{p_min_lat} AND :{p_max_lat}"
            f" AND a.gps_lon BETWEEN :{p_min_lon} AND :{p_max_lon}"
        )

    def to_url_value(self) -> str:
        return f"{self.lat},{self.lon},{self.radius_km}"

    @classmethod
    def from_url_value(cls, raw: str) -> NearLocation:
        parts = raw.split(",")
        if len(parts) < 2:
            raise ValueError(f"NearLocation requires lat,lon[,radius]: {raw}")
        lat = float(parts[0])
        lon = float(parts[1])
        radius = float(parts[2]) if len(parts) > 2 else 1.0
        return cls(lat=lat, lon=lon, radius_km=radius)


@dataclass(frozen=True)
class DateRange(LeafFilter):
    from_dt: datetime | None = None
    to_dt: datetime | None = None

    @classmethod
    def prefix(cls) -> str:
        return "date"

    @classmethod
    def type_name(cls) -> str:
        return "date"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.DATE_RANGE

    def label(self) -> str:
        fmt = "%Y-%m-%d"
        if self.from_dt and self.to_dt:
            return f"{self.from_dt.strftime(fmt)} \u2013 {self.to_dt.strftime(fmt)}"
        if self.from_dt:
            return f"From {self.from_dt.strftime(fmt)}"
        return f"Until {self.to_dt.strftime(fmt)}"  # type: ignore[union-attr]

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.from_dt is not None:
            p = self._param_name("date_from", counter)
            params[p] = self.from_dt
            parts.append(f"COALESCE(a.taken_at, a.file_mtime) >= :{p}")
        if self.to_dt is not None:
            p = self._param_name("date_to", counter)
            # to_dt is exclusive upper bound (next day)
            params[p] = self.to_dt + timedelta(days=1)
            parts.append(f"COALESCE(a.taken_at, a.file_mtime) < :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        fmt = "%Y-%m-%d"
        f = self.from_dt.strftime(fmt) if self.from_dt else ""
        t = self.to_dt.strftime(fmt) if self.to_dt else ""
        return f"{f},{t}"

    @classmethod
    def from_url_value(cls, raw: str) -> DateRange:
        parts = raw.split(",", 1)
        from_dt = None
        to_dt = None
        if parts[0]:
            from_dt = datetime.strptime(parts[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if len(parts) > 1 and parts[1]:
            to_dt = datetime.strptime(parts[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return cls(from_dt=from_dt, to_dt=to_dt)


# --- Rating filters (need rating JOIN) ---

@dataclass(frozen=True)
class Favorite(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "favorite"

    @classmethod
    def type_name(cls) -> str:
        return "favorite"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Favorites" if self.value else "Not favorites"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "r.favorite = TRUE"
        return "(r.favorite IS NULL OR r.favorite = FALSE)"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> Favorite:
        return cls(value=raw.lower() in ("yes", "true", "1"))

    @property
    def needs_rating_join(self) -> bool:
        return True


@dataclass(frozen=True)
class StarRange(LeafFilter):
    min_val: int | None = None
    max_val: int | None = None

    @classmethod
    def prefix(cls) -> str:
        return "stars"

    @classmethod
    def type_name(cls) -> str:
        return "stars"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.INT_RANGE

    def label(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return f"{self.min_val} star{'s' if self.min_val != 1 else ''}"
            return f"{self.min_val}\u2013{self.max_val} stars"
        if self.min_val is not None:
            return f"{self.min_val}+ stars"
        return f"\u2264{self.max_val} stars"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        parts = []
        if self.min_val is not None:
            p = self._param_name("star_min", counter)
            params[p] = self.min_val
            parts.append(f"COALESCE(r.stars, 0) >= :{p}")
        if self.max_val is not None:
            p = self._param_name("star_max", counter)
            params[p] = self.max_val
            parts.append(f"COALESCE(r.stars, 0) <= :{p}")
        return " AND ".join(parts) if parts else "TRUE"

    def to_url_value(self) -> str:
        if self.min_val is not None and self.max_val is not None:
            if self.min_val == self.max_val:
                return str(self.min_val)
            return f"{self.min_val}-{self.max_val}"
        if self.min_val is not None:
            return f"{self.min_val}+"
        return f"-{self.max_val}"

    @classmethod
    def from_url_value(cls, raw: str) -> StarRange:
        lo, hi = _parse_range(raw)
        return cls(
            min_val=int(lo) if lo else None,
            max_val=int(hi) if hi else None,
        )

    @property
    def needs_rating_join(self) -> bool:
        return True


@dataclass(frozen=True)
class ColorLabel(LeafFilter):
    colors: tuple[str, ...] = ()

    @classmethod
    def prefix(cls) -> str:
        return "color"

    @classmethod
    def type_name(cls) -> str:
        return "color"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.ENUM

    @classmethod
    def enum_values(cls) -> list[str] | None:
        return ["red", "orange", "yellow", "green", "blue", "purple"]

    def label(self) -> str:
        if len(self.colors) == 1:
            return self.colors[0].capitalize()
        return "Multiple colors"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        placeholders = []
        for c in self.colors:
            p = self._param_name("color", counter)
            params[p] = c
            placeholders.append(f":{p}")
        return f"r.color IN ({', '.join(placeholders)})"

    def to_url_value(self) -> str:
        return ",".join(self.colors)

    @classmethod
    def from_url_value(cls, raw: str) -> ColorLabel:
        colors = tuple(c.strip() for c in raw.split(",") if c.strip())
        return cls(colors=colors)

    @property
    def needs_rating_join(self) -> bool:
        return True


@dataclass(frozen=True)
class HasRating(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "has_rating"

    @classmethod
    def type_name(cls) -> str:
        return "has_rating"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Has rating" if self.value else "No rating"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "r.user_id IS NOT NULL"
        return "r.user_id IS NULL"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> HasRating:
        return cls(value=raw.lower() in ("yes", "true", "1"))

    @property
    def needs_rating_join(self) -> bool:
        return True


@dataclass(frozen=True)
class HasColor(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "has_color"

    @classmethod
    def type_name(cls) -> str:
        return "has_color"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Has color label" if self.value else "No color label"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "r.color IS NOT NULL"
        return "(r.user_id IS NULL OR r.color IS NULL)"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> HasColor:
        return cls(value=raw.lower() in ("yes", "true", "1"))

    @property
    def needs_rating_join(self) -> bool:
        return True


# --- Face / people filters ---

@dataclass(frozen=True)
class HasFaces(LeafFilter):
    value: bool = True

    @classmethod
    def prefix(cls) -> str:
        return "has_faces"

    @classmethod
    def type_name(cls) -> str:
        return "has_faces"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.BOOLEAN

    def label(self) -> str:
        return "Has faces" if self.value else "No faces"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        if self.value:
            return "a.face_count > 0"
        return "(a.face_count IS NULL OR a.face_count = 0)"

    def to_url_value(self) -> str:
        return "yes" if self.value else "no"

    @classmethod
    def from_url_value(cls, raw: str) -> HasFaces:
        return cls(value=raw.lower() in ("yes", "true", "1"))


@dataclass(frozen=True)
class PersonFilter(LeafFilter):
    person_id: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "person"

    @classmethod
    def type_name(cls) -> str:
        return "person"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    def label(self) -> str:
        return "Person filter"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("person_id", counter)
        params[p] = self.person_id
        return f"a.asset_id IN (SELECT asset_id FROM faces WHERE person_id = :{p})"

    def to_url_value(self) -> str:
        return self.person_id

    @classmethod
    def from_url_value(cls, raw: str) -> PersonFilter:
        return cls(person_id=raw)


@dataclass(frozen=True)
class TagFilter(LeafFilter):
    value: str = ""

    @classmethod
    def prefix(cls) -> str:
        return "tag"

    @classmethod
    def type_name(cls) -> str:
        return "tag"

    @classmethod
    def value_kind(cls) -> ValueKind:
        return ValueKind.STRING

    @classmethod
    def faceted(cls) -> bool:
        return True

    def label(self) -> str:
        return f"#{self.value}"

    def to_sql(self, params: dict, counter: list[int]) -> str:
        p = self._param_name("tag", counter)
        params[p] = self.value
        return f"m.data->'tags' @> jsonb_build_array(:{p})"

    def to_url_value(self) -> str:
        return self.value

    @classmethod
    def from_url_value(cls, raw: str) -> TagFilter:
        return cls(value=raw)

    @property
    def needs_metadata_join(self) -> bool:
        return True
