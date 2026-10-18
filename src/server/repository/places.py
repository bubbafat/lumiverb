"""Places by name (ADR-021's Place names plug-in, ADR-017 phase 4): the
GeoNames list of towns in the control database, the same for every
account, with their regions and countries. Replaced whole, in one
transaction, so a search never sees half a list.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlmodel import Session

PREFIX = "geonames:"
_BATCH = 5000


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


class PlaceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def replace(self, places: Iterable[dict[str, Any]], admin1: Iterable[dict[str, Any]],
                countries: Iterable[dict[str, Any]]) -> int:
        """The whole list, instead of what was there, in one transaction (the
        caller commits). Returns how many places."""
        self._session.execute(text("DELETE FROM geonames_places"))
        self._session.execute(text("DELETE FROM geonames_admin1"))
        self._session.execute(text("DELETE FROM geonames_countries"))
        n = 0
        for table, cols, rows in (
            ("geonames_countries", ("iso", "name", "folded"), countries),
            ("geonames_admin1", ("code", "name", "folded"), admin1),
            ("geonames_places", ("geonameid", "name", "ascii_name", "folded", "lat", "lon", "feature_code",
                                 "country_code", "admin1_code", "population", "timezone"), places),
        ):
            sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
                       " ON CONFLICT DO NOTHING")
            batch: list[dict[str, Any]] = []
            for row in rows:
                batch.append({**row, "folded": fold(row["name"])})
                if table == "geonames_places":
                    n += 1
                if len(batch) >= _BATCH:
                    self._session.execute(sql, batch)
                    batch = []
            if batch:
                self._session.execute(sql, batch)
        return n

    def count(self) -> int:
        return int(self._session.execute(text("SELECT count(*) FROM geonames_places")).scalar() or 0)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Places whose name starts with what's typed, accents or not (folded:
        "zurich" finds Zürich), or GeoNames' ASCII spelling does ("Zuerich");
        the exact name first, then the most populous. "Name, where" narrows it
        to a region or country starting so (or a country's code)."""
        name, _, where = query.partition(",")
        name, where = fold(name.strip()), fold(where.strip())
        if not name:
            return []
        rows = self._session.execute(text(
            f"{_SELECT} WHERE (p.folded LIKE :prefix ESCAPE '\\' OR lower(p.ascii_name) LIKE :prefix ESCAPE '\\')"
            "   AND (:where = '' OR c.folded LIKE :where_prefix ESCAPE '\\'"
            "        OR a.folded LIKE :where_prefix ESCAPE '\\' OR lower(p.country_code) = :where)"
            " ORDER BY (p.folded = :exact OR lower(p.ascii_name) = :exact) DESC, p.population DESC,"
            "   p.geonameid LIMIT :limit"
        ), {"prefix": _like(name), "exact": name, "where": where, "where_prefix": _like(where),
            "limit": int(limit)}).mappings().all()
        return [dict(r) for r in rows]

    def get(self, geonameid: int) -> dict[str, Any] | None:
        row = self._session.execute(text(f"{_SELECT} WHERE p.geonameid = :g"), {"g": geonameid}).mappings().first()
        return dict(row) if row else None
