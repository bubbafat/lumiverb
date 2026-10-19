"""Places by name (ADR-021's Place names plug-in, ADR-017 phase 4): the
GeoNames list of towns in the control database, the same for every
account, with their regions and countries, and its landmarks
(src/plugins/geonames/landmarks.py), each found by its names in other
languages too (geonames_names). Replaced whole, in one transaction, so a
search never sees half a list.

Since ADR-023 phase 3 the GeoNames plug-in keeps its own list; this one,
fetched by the built-in Place names before, is what part 3's fallbacks
answer from while the plug-in can't (not installed, not running, fetching
its first list), in an account where GeoNames is on by its own switch
(on_here). It goes in part 4.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

from src.plugins.geonames.landmarks import FARTHEST_M, KINDS, reach_sql

PLUGIN = "geonames"
PREFIX = "geonames:"
_BATCH = 5000
# What's a town's name: not a section of one ("Zürich (Kreis 7)"), nor a
# historical, abandoned or destroyed place (as ix_geonames_places_lat leaves out).
TOWNS = "p.feature_code NOT IN ('PPLX', 'PPLH', 'PPLQ', 'PPLW', 'PPLCH')"
EARTH_M = 6_371_000.0


def _meters(t: str) -> str:
    """SQL: meters between (t.lat, t.lon) and (:lat, :lon), the haversine way."""
    return (f"2 * 6371000.0 * asin(sqrt(LEAST(1.0, power(sin(radians({t}.lat - :lat) / 2), 2)"
            f" + cos(radians(:lat)) * cos(radians({t}.lat)) * power(sin(radians({t}.lon - :lon) / 2), 2))))")


def on_here_sql() -> str:
    """SQL true while GeoNames is on in the account by its own switch (its plugins row: enabled, the version
    accepted standing under the acknowledgment shipped, src/own_plugins/geonames/), installed or not: where its
    old names and this list stand in for the plug-in (part 3's fallbacks)."""
    from src.server.own_plugins import shipped
    from src.server.repository.plugins import on_sql

    manifest = shipped(PLUGIN)
    return on_sql(PLUGIN, spec=manifest.spec()) if manifest else "false"


def on_here(session: Session) -> bool:
    return bool(session.execute(text(f"SELECT {on_here_sql()}")).scalar())


def fold(name: str) -> str:
    """A name as it's searched: lowercase, accents dropped ("Zürich" → "zurich",
    "São Paulo" → "sao paulo"). Saved with each name and applied to what's typed."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def place_id(geonameid: int) -> str:
    return f"{PREFIX}{geonameid}"


def geonameid_of(place: str) -> int | None:
    """A place_id's GeoNames id, or None for one that isn't."""
    if not place.startswith(PREFIX) or not place[len(PREFIX):].isdigit():
        return None
    return int(place[len(PREFIX):])


def label(row: dict[str, Any]) -> str:
    """"Port of Spain, Trinidad and Tobago": the name, its region unless it's the same, its country."""
    parts = [row["name"]]
    if row.get("region") and row["region"] != row["name"]:
        parts.append(row["region"])
    if row.get("country"):
        parts.append(row["country"])
    return ", ".join(parts)


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


_SELECT = (
    "SELECT p.geonameid, p.name, p.lat, p.lon, p.population, p.country_code, a.name AS region, c.name AS country"
    " FROM geonames_places p"
    " LEFT JOIN geonames_admin1 a ON a.code = p.country_code || '.' || p.admin1_code"
    " LEFT JOIN geonames_countries c ON c.iso = p.country_code"
)
_LANDMARKS = (
    "SELECT l.geonameid, l.name, l.lat, l.lon, 0 AS population, l.country_code, l.feature_code,"
    " a.name AS region, c.name AS country"
    " FROM geonames_landmarks l"
    " LEFT JOIN geonames_admin1 a ON a.code = l.country_code || '.' || l.admin1_code"
    " LEFT JOIN geonames_countries c ON c.iso = l.country_code"
)


# An other name's tier in a search (search): exactly what's typed, English 0 (as the place's own name),
# another language 1; starting so, English 2 (as its own), another 3.
_TIER = ("CASE WHEN n.folded = :exact THEN CASE WHEN n.language = 'en' THEN 0 ELSE 1 END"
         " ELSE CASE WHEN n.language = 'en' THEN 2 ELSE 3 END END")


def _kind(row: Any) -> dict[str, Any]:
    """A row as a dict, with its kind's noun when it's a landmark (None for a town)."""
    out = dict(row)
    code = out.pop("feature_code", None)
    out["kind"] = KINDS[code].noun if code in KINDS else None
    return out


def _band(lat: float, lon: float, within_m: float, t: str) -> tuple[str, dict[str, Any]]:
    """SQL and its parameters: t's rows in a band of latitude around the point (indexed), then of
    longitude as wide in meters (both sides of the antimeridian)."""
    dlat = math.degrees(within_m / EARTH_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 1e-6)
    params: dict[str, Any] = {"lat": lat, "lon": lon, "lo": lat - dlat, "hi": lat + dlat}
    if dlon >= 180:
        lons = "TRUE"
    else:
        west, east = lon - dlon, lon + dlon
        ranges = [(max(west, -180.0), min(east, 180.0))]
        if west < -180:
            ranges.append((west + 360, 180.0))
        if east > 180:
            ranges.append((-180.0, east - 360))
        lons = " OR ".join(f"{t}.lon BETWEEN :w{i} AND :e{i}" for i in range(len(ranges)))
        for i, (w, e) in enumerate(ranges):
            params[f"w{i}"], params[f"e{i}"] = w, e
    return f"{t}.lat BETWEEN :lo AND :hi AND ({lons})", params


class PlaceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def replace(self, places: Iterable[dict[str, Any]], admin1: Iterable[dict[str, Any]],
                countries: Iterable[dict[str, Any]], landmarks: Iterable[dict[str, Any]] = (),
                names: Iterable[dict[str, Any]] = ()) -> int:
        """The whole list, instead of what was there, in one transaction (the
        caller commits): names are the places' other names ({geonameid, name,
        folded, language}; a name twice for a place kept once). Returns how many places:
        towns and landmarks."""
        for table in ("geonames_places", "geonames_landmarks", "geonames_admin1", "geonames_countries",
                      "geonames_names"):
            self._session.execute(text(f"DELETE FROM {table}"))
        n = 0
        for table, cols, rows in (
            ("geonames_countries", ("iso", "name", "folded"), countries),
            ("geonames_admin1", ("code", "name", "folded"), admin1),
            ("geonames_places", ("geonameid", "name", "ascii_name", "folded", "lat", "lon", "feature_code",
                                 "country_code", "admin1_code", "population", "timezone"), places),
            ("geonames_landmarks", ("geonameid", "name", "ascii_name", "folded", "lat", "lon", "feature_code",
                                    "country_code", "admin1_code"), landmarks),
            ("geonames_names", ("geonameid", "name", "folded", "language"), names),
        ):
            # A place's name in several languages is kept once: in English if it's English's too (the app's,
            # which search ranks first), as GeoNames lists it in whatever order.
            conflict = (" ON CONFLICT (geonameid, folded) DO UPDATE SET name = EXCLUDED.name,"
                        " language = EXCLUDED.language WHERE EXCLUDED.language = 'en'"
                        if table == "geonames_names" else " ON CONFLICT DO NOTHING")
            sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
                       + conflict)
            batch: dict[Any, dict[str, Any]] = {}
            for row in rows:
                row = {**row, "folded": row.get("folded") or fold(row["name"])}
                # Once a statement: by key for the names (English kept), each row for the rest.
                key = (row["geonameid"], row["folded"]) if table == "geonames_names" else len(batch)
                if key not in batch or row.get("language") == "en":
                    batch[key] = row
                if table in ("geonames_places", "geonames_landmarks"):
                    n += 1
                if len(batch) >= _BATCH:
                    self._session.execute(sql, list(batch.values()))
                    batch = {}
            if batch:
                self._session.execute(sql, list(batch.values()))
        # How known each landmark is: how many other names it has (they have no population to rank by).
        self._session.execute(text(
            "UPDATE geonames_landmarks l SET known = c.n FROM (SELECT geonameid, count(*) AS n FROM geonames_names"
            " GROUP BY geonameid) c WHERE c.geonameid = l.geonameid"))
        return n

    def count(self) -> int:
        return int(self._session.execute(text("SELECT count(*) FROM geonames_places")).scalar() or 0)

    def kept(self) -> bool:
        """Whether the list is here (a search's question, every time: no count)."""
        return bool(self._session.execute(text("SELECT EXISTS (SELECT 1 FROM geonames_places)")).scalar())

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Towns and landmarks whose name starts with what's typed, accents or
        not (folded: "zurich" finds Zürich), or GeoNames' ASCII spelling does
        ("Zuerich"), or one of its names in other languages does ("Eiffel
        Tower" finds Tour Eiffel; matched says which when its own didn't).
        Ranked by how well a place's best name matched (tier: exactly its own
        or English name, the app's language, as New York City is "New York";
        exactly another; its own or English name starts so; another does), then
        the most populous, landmarks after towns, the best known first (the
        most other names). "Name, where" narrows it to a region or country
        starting so (or a country's code). Each with its kind (None: a town)."""
        name, _, where = query.partition(",")
        name, where = fold(name.strip()), fold(where.strip())
        if not name:
            return []
        own = "({t}.folded LIKE :prefix ESCAPE '\\' OR lower({t}.ascii_name) LIKE :prefix ESCAPE '\\')"
        exact = "({t}.folded = :exact OR lower({t}.ascii_name) = :exact)"
        cols = "{t}.geonameid, {t}.name, {t}.lat, {t}.lon, {pop}, {t}.country_code, {t}.admin1_code, {code}, {known},"
        town = cols.format(t="p", pop="p.population", code="NULL::text AS feature_code", known="0 AS known")
        mark = cols.format(t="l", pop="0", code="l.feature_code", known="l.known")
        rows = self._session.execute(text(
            "WITH found AS ("
            f"  SELECT {town} NULL::text AS matched, NULL::text AS language,"
            f"    CASE WHEN {exact.format(t='p')} THEN 0 ELSE 2 END AS tier"
            "     FROM geonames_places p WHERE " + own.format(t="p") +
            f"  UNION ALL SELECT {mark} NULL, NULL, CASE WHEN {exact.format(t='l')} THEN 0 ELSE 2 END"
            "     FROM geonames_landmarks l WHERE " + own.format(t="l") +
            f"  UNION ALL SELECT {town} n.name, n.language, {_TIER}"
            "     FROM geonames_names n JOIN geonames_places p ON p.geonameid = n.geonameid"
            "     WHERE n.folded LIKE :prefix ESCAPE '\\'"
            f"  UNION ALL SELECT {mark} n.name, n.language, {_TIER}"
            "     FROM geonames_names n JOIN geonames_landmarks l ON l.geonameid = n.geonameid"
            "     WHERE n.folded LIKE :prefix ESCAPE '\\'"
            "), best AS ("
            # Each place once, ranked by its best name's tier: shown by its own name when that matched, else
            # its other name that matched best (by tier, then English, then the shortest).
            "  SELECT DISTINCT ON (geonameid) *, min(tier) OVER (PARTITION BY geonameid) AS best_tier"
            "  FROM found"
            "  ORDER BY geonameid, matched IS NOT NULL, tier, language = 'en' DESC, length(matched), matched"
            ")"
            " SELECT f.geonameid, f.name, f.lat, f.lon, f.population, f.country_code, f.feature_code, f.matched,"
            "   a.name AS region, c.name AS country"
            " FROM best f"
            " LEFT JOIN geonames_admin1 a ON a.code = f.country_code || '.' || f.admin1_code"
            " LEFT JOIN geonames_countries c ON c.iso = f.country_code"
            " WHERE (:where = '' OR c.folded LIKE :where_prefix ESCAPE '\\'"
            "        OR a.folded LIKE :where_prefix ESCAPE '\\' OR lower(f.country_code) = :where)"
            " ORDER BY f.best_tier, f.population DESC, f.feature_code IS NOT NULL, f.known DESC, f.geonameid"
            " LIMIT :limit"
        ), {"prefix": _like(name), "exact": name, "where": where, "where_prefix": _like(where),
            "limit": int(limit)}).mappings().all()
        return [_kind(r) for r in rows]

    def nearest(self, lat: float, lon: float, within_m: float) -> dict[str, Any] | None:
        """The nearest town to a point, within within_m meters, with how far
        (distance_m); None when there's none that near."""
        band, params = _band(lat, lon, within_m, "p")
        measured = _SELECT.replace("SELECT ", f"SELECT {_meters('p')} AS distance_m, ", 1)
        row = self._session.execute(text(
            f"SELECT * FROM ({measured} WHERE {TOWNS} AND {band}) d"
            " WHERE d.distance_m <= :within ORDER BY d.distance_m, d.population DESC LIMIT 1"
        ), {**params, "within": float(within_m)}).mappings().first()
        return dict(row) if row else None

    def landmark_at(self, lat: float, lon: float, sure_m: float = 0) -> dict[str, Any] | None:
        """The landmark a point is at: within its kind's reach (landmarks.py),
        for a point sure to within sure_m meters (none for one less sure than
        the reach), the nearest for its reach where there are several. With
        how far (distance_m) and its kind; None: at none."""
        band, params = _band(lat, lon, FARTHEST_M, "l")
        measured = _LANDMARKS.replace("SELECT ", f"SELECT {_meters('l')} AS distance_m, {reach_sql()} AS reach_m, ", 1)
        row = self._session.execute(text(
            f"SELECT * FROM ({measured} WHERE {band}) d"
            " WHERE d.distance_m <= d.reach_m AND d.reach_m >= :sure"
            " ORDER BY d.distance_m / d.reach_m, d.distance_m, d.geonameid LIMIT 1"
        ), {**params, "sure": float(sure_m)}).mappings().first()
        return _kind(row) if row else None

    def get(self, geonameid: int) -> dict[str, Any] | None:
        """A town or a landmark by its GeoNames id, with its kind (None: a town)."""
        row = self._session.execute(text(f"{_SELECT} WHERE p.geonameid = :g"), {"g": geonameid}).mappings().first()
        if row is None:
            row = self._session.execute(text(f"{_LANDMARKS} WHERE l.geonameid = :g"),
                                        {"g": geonameid}).mappings().first()
        return _kind(row) if row else None
