"""Its place list: a SQLite file in its data folder, built once from
GeoNames' files (fetch.py) and then only read. The towns of 1,000 people or
more with their regions and countries, the landmarks (landmarks.py) and the
names of both in other languages, with what's searched worked out once
(each name folded: lowercase, accents dropped).

Never in WAL mode: built with SQLite's default journal and read with
?mode=ro&immutable=1, a connection a request, so a list put in place of
another while requests read is never paired with the other's -wal file.

Each answer is found as Lumiverb's built-in Place names finds it in
Postgres (src/server/repository/places.py, until it goes): the same band of
latitude and longitude, the same arithmetic for distance, the same tiers
and order for a search, a full tie decided by the lowest id. The parity
tests hold the two to the same answers.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import unicodedata
import urllib.parse
from collections.abc import Iterable, Iterator
from typing import Any

from landmarks import FARTHEST_M, KINDS

# What's in a list: a list another version of this plug-in built is built again (and used meanwhile).
DATA_VERSION = 1
EARTH_M = 6_371_000.0
# What's a town's name: not a section of one ("Zürich (Kreis 7)"), nor a historical, abandoned or destroyed place.
TOWNS = "t.feature_code NOT IN ('PPLX', 'PPLH', 'PPLQ', 'PPLW', 'PPLCH')"
# The most of each that Lumiverb takes in an answer (its checks of a place and of a fact).
NAME_MAX, WHERE_MAX, KIND_MAX, FACT_MAX = 200, 100, 60, 200
# What Lumiverb shows no name with: a control character, a tag's < and >.
_UNSHOWN = re.compile("[\x00-\x1f\x7f-\x9f<>]")
_LAST = "\U0010ffff"  # after every character: a prefix's names sort between it and it + _LAST

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE countries (iso TEXT PRIMARY KEY, name TEXT NOT NULL, folded TEXT NOT NULL);
CREATE TABLE regions (code TEXT PRIMARY KEY, name TEXT NOT NULL, folded TEXT NOT NULL);
CREATE TABLE towns (geonameid INTEGER PRIMARY KEY, name TEXT NOT NULL, ascii_lower TEXT NOT NULL,
    folded TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, feature_code TEXT NOT NULL,
    country_code TEXT NOT NULL, admin1_code TEXT NOT NULL, population INTEGER NOT NULL);
CREATE TABLE landmarks (geonameid INTEGER PRIMARY KEY, name TEXT NOT NULL, ascii_lower TEXT NOT NULL,
    folded TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, feature_code TEXT NOT NULL,
    country_code TEXT NOT NULL, admin1_code TEXT NOT NULL, known INTEGER NOT NULL DEFAULT 0);
CREATE TABLE names (geonameid INTEGER NOT NULL, name TEXT NOT NULL, folded TEXT NOT NULL,
    language TEXT NOT NULL, PRIMARY KEY (geonameid, folded));
"""
# Made once the rows are in (faster than kept up as they come).
_INDEXES = f"""
CREATE INDEX towns_at ON towns (lat, lon) WHERE {TOWNS.replace('t.', '')};
CREATE INDEX towns_folded ON towns (folded);
CREATE INDEX towns_ascii ON towns (ascii_lower);
CREATE INDEX landmarks_at ON landmarks (lat, lon);
CREATE INDEX landmarks_folded ON landmarks (folded);
CREATE INDEX landmarks_ascii ON landmarks (ascii_lower);
CREATE INDEX names_folded ON names (folded);
"""


def fold(name: str) -> str:
    """A name as it's searched: lowercase, accents dropped ("Zürich" → "zurich",
    "São Paulo" → "sao paulo"). Saved with each name and applied to what's typed."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def clean(text: str) -> str:
    """A name with nothing in it Lumiverb refuses to show."""
    return _UNSHOWN.sub("", text)


def cut(text: str | None, most: int) -> str | None:
    """At most `most` characters: a longer one cut at a word, with "…"."""
    if text is None or len(text) <= most:
        return text
    kept = text[:most - 1]
    if not (text[most - 1].isspace() or kept[-1:].isspace()) and kept.rfind(" ") > 0:
        kept = kept[:kept.rfind(" ")]  # cut inside a word: back to before it
    return kept.rstrip(" ,;:") + "…"


def label(row: dict[str, Any]) -> str:
    """"Port of Spain, Trinidad and Tobago": the name, its region unless it's the same, its country."""
    parts = [row["name"]]
    if row.get("region") and row["region"] != row["name"]:
        parts.append(row["region"])
    if row.get("country"):
        parts.append(row["country"])
    return ", ".join(parts)


def is_fix(lat: float, lon: float) -> bool:
    """A real point: finite, in range, and not (0, 0)."""
    return (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def meters(lat1: float, lon1: float, lat: float, lon: float) -> float:
    """Meters between (lat1, lon1) and (lat, lon), the haversine way, in Postgres's arithmetic as the
    built-in's query has it (double precision: radians, sin, cos, power, asin)."""
    a = (math.pow(math.sin(math.radians(lat1 - lat) / 2), 2)
         + math.cos(math.radians(lat)) * math.cos(math.radians(lat1)) * math.pow(math.sin(math.radians(lon1 - lon) / 2), 2))
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, a)))


def _reach(code: str) -> int:
    kind = KINDS.get(code)
    return kind.reach_m if kind else 0


def band(lat: float, lon: float, within_m: float, t: str) -> tuple[str, dict[str, Any]]:
    """SQL and its parameters: t's rows in a band of latitude around the point (indexed), then of
    longitude as wide in meters (both sides of the antimeridian)."""
    dlat = math.degrees(within_m / EARTH_M)
    dlon = dlat / max(math.cos(math.radians(lat)), 1e-6)
    params: dict[str, Any] = {"lat": lat, "lon": lon, "lo": lat - dlat, "hi": lat + dlat}
    if dlon >= 180:
        lons = "1"
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


# ---------------------------------------------------------------------------
# Building a list
# ---------------------------------------------------------------------------


class Builder:
    """A list being built at path (what was there removed first): its rows written as they're read, each
    name cleaned of what Lumiverb won't show and a place with no name left or no real point left out; its
    indexes made at the end."""

    def __init__(self, path: str) -> None:
        for leftover in (path, path + "-journal"):
            if os.path.exists(leftover):
                os.unlink(leftover)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA synchronous = OFF")  # a build cut off is thrown away; it's synced once whole
        self._db.executescript(_SCHEMA)
        # Each place's own names, folded, for its other names (fetch.parse_names).
        self.own: dict[int, tuple[str, ...]] = {}

    def countries(self, rows: Iterable[dict[str, str]]) -> None:
        self._db.executemany("INSERT OR IGNORE INTO countries VALUES (?, ?, ?)",
                             ((r["iso"], n, fold(n)) for r in rows if (n := clean(r["name"])).strip()))

    def regions(self, rows: Iterable[dict[str, str]]) -> None:
        self._db.executemany("INSERT OR IGNORE INTO regions VALUES (?, ?, ?)",
                             ((r["code"], n, fold(n)) for r in rows if (n := clean(r["name"])).strip()))

    def _kept(self, rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for r in rows:
            name = clean(r["name"])
            if not name.strip() or not is_fix(r["lat"], r["lon"]):
                continue
            ascii_lower = (clean(r["ascii_name"]) or name).lower()
            row = {**r, "name": name, "ascii_lower": ascii_lower, "folded": fold(name)}
            self.own[r["geonameid"]] = (fold(name), r["ascii_name"].lower())
            yield row

    def towns(self, rows: Iterable[dict[str, Any]]) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO towns VALUES (:geonameid, :name, :ascii_lower, :folded, :lat, :lon, :feature_code,"
            " :country_code, :admin1_code, :population)", self._kept(rows))

    def landmarks(self, rows: Iterable[dict[str, Any]]) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO landmarks VALUES (:geonameid, :name, :ascii_lower, :folded, :lat, :lon,"
            " :feature_code, :country_code, :admin1_code, 0)", self._kept(rows))

    def names(self, rows: Iterable[dict[str, Any]]) -> None:
        """The places' other names ({geonameid, name, folded, language}): a name in several languages kept once,
        in English if it's English's too (the app's, which search ranks first), as GeoNames lists them in
        whatever order."""

        def cleaned() -> Iterator[tuple[int, str, str, str]]:
            for r in rows:
                name = clean(r["name"])
                folded = fold(name) if name != r["name"] else r["folded"]
                if name.strip() and folded:
                    yield r["geonameid"], name, folded, r["language"]

        self._db.executemany(
            "INSERT INTO names VALUES (?, ?, ?, ?) ON CONFLICT (geonameid, folded) DO UPDATE SET"
            " name = excluded.name, language = excluded.language WHERE excluded.language = 'en'", cleaned())

    def counts(self) -> dict[str, int]:
        return {table: self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 — its own
                for table in ("towns", "landmarks", "names", "regions", "countries")}

    def finish(self, meta: dict[str, Any]) -> dict[str, int]:
        """Its indexes, each landmark's known (how many other names it has: they have no population to rank
        by), what it is (meta) and statistics; then it's synced to disk and closed. Returns its counts."""
        self._db.executescript(_INDEXES)
        self._db.execute("UPDATE landmarks SET known = (SELECT count(*) FROM names n WHERE n.geonameid ="
                         " landmarks.geonameid)")
        counts = self.counts()
        meta = {**meta, "data_version": DATA_VERSION, **{f"{t}_count": n for t, n in counts.items()}}
        self._db.executemany("INSERT INTO meta VALUES (?, ?)", ((k, str(v)) for k, v in meta.items()))
        self._db.commit()
        self._db.execute("ANALYZE")
        self._db.commit()
        self._db.close()
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return counts

    def abandon(self) -> None:
        self._db.close()


# ---------------------------------------------------------------------------
# Reading the list in use
# ---------------------------------------------------------------------------

_TOWN = ("t.geonameid, t.name, t.lat, t.lon, t.population, t.country_code, t.admin1_code, NULL AS feature_code,"
         " 0 AS known")
_MARK = ("l.geonameid, l.name, l.lat, l.lon, 0 AS population, l.country_code, l.admin1_code, l.feature_code,"
         " l.known")
_OWN = "(({t}.folded >= :p AND {t}.folded < :p_end) OR ({t}.ascii_lower >= :p AND {t}.ascii_lower < :p_end))"
_EXACT = "({t}.folded = :exact OR {t}.ascii_lower = :exact)"
# An other name's tier in a search: exactly what's typed, English 0 (as the place's own name), another
# language 1; starting so, English 2 (as its own), another 3.
_TIER = ("CASE WHEN n.folded = :exact THEN CASE WHEN n.language = 'en' THEN 0 ELSE 1 END"
         " ELSE CASE WHEN n.language = 'en' THEN 2 ELSE 3 END END")
_OTHER = "n.folded >= :p AND n.folded < :p_end"
_SEARCH = (
    "WITH found AS ("
    f"  SELECT {_TOWN}, NULL AS matched, NULL AS language,"
    f"    CASE WHEN {_EXACT.format(t='t')} THEN 0 ELSE 2 END AS tier FROM towns t WHERE {_OWN.format(t='t')}"
    f"  UNION ALL SELECT {_MARK}, NULL, NULL, CASE WHEN {_EXACT.format(t='l')} THEN 0 ELSE 2 END"
    f"    FROM landmarks l WHERE {_OWN.format(t='l')}"
    f"  UNION ALL SELECT {_TOWN}, n.name, n.language, {_TIER}"
    f"    FROM names n JOIN towns t ON t.geonameid = n.geonameid WHERE {_OTHER}"
    f"  UNION ALL SELECT {_MARK}, n.name, n.language, {_TIER}"
    f"    FROM names n JOIN landmarks l ON l.geonameid = n.geonameid WHERE {_OTHER}"
    "), best AS ("
    # Each place once, ranked by its best name's tier: shown by its own name when that matched, else its other
    # name that matched best (by tier, then English, then the shortest, then by its characters).
    "  SELECT *, ROW_NUMBER() OVER (PARTITION BY geonameid ORDER BY matched IS NOT NULL, tier,"
    "    language = 'en' DESC, length(matched), matched) AS nth,"
    "    MIN(tier) OVER (PARTITION BY geonameid) AS best_tier"
    "  FROM found"
    ")"
    " SELECT f.geonameid, f.name, f.lat, f.lon, f.population, f.country_code, f.feature_code, f.matched,"
    "   r.name AS region, c.name AS country"
    " FROM best f"
    " LEFT JOIN regions r ON r.code = f.country_code || '.' || f.admin1_code"
    " LEFT JOIN countries c ON c.iso = f.country_code"
    " WHERE f.nth = 1 AND (:where = '' OR (c.folded >= :w AND c.folded < :w_end)"
    "   OR (r.folded >= :w AND r.folded < :w_end) OR lower(f.country_code) = :where)"
    " ORDER BY f.best_tier, f.population DESC, f.feature_code IS NOT NULL, f.known DESC, f.geonameid"
    " LIMIT :limit"
)
_WHERE = (" LEFT JOIN regions r ON r.code = {t}.country_code || '.' || {t}.admin1_code"
          " LEFT JOIN countries c ON c.iso = {t}.country_code")


def _kind(row: sqlite3.Row) -> dict[str, Any]:
    """A row as a dict, with its kind's noun when it's a landmark (None for a town)."""
    out = dict(row)
    code = out.pop("feature_code", None)
    out["kind"] = KINDS[code].noun if code in KINDS else None
    return out


def _uri(path: str) -> str:
    return "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro&immutable=1"


class Places:
    """The list in use, read only: one for a request (or a batch of them)."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(_uri(path), uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.create_function("meters", 4, meters, deterministic=True)
        self._db.create_function("reach", 1, _reach, deterministic=True)

    def __enter__(self) -> Places:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta").fetchall())

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
        name, _, where = query.encode("utf-8", "replace").decode("utf-8").partition(",")
        name, where = fold(name.strip()), fold(where.strip())
        if not name:
            return []
        rows = self._db.execute(_SEARCH, {"p": name, "p_end": name + _LAST, "exact": name, "where": where,
                                          "w": where, "w_end": where + _LAST, "limit": int(limit)}).fetchall()
        return [_kind(r) for r in rows]

    def nearest(self, lat: float, lon: float, within_m: float) -> dict[str, Any] | None:
        """The nearest town to a point, within within_m meters, with how far (distance_m); None when there's
        none that near. Equally near, the most populous, then the lowest id."""
        where, params = band(lat, lon, within_m, "t")
        row = self._db.execute(
            "SELECT * FROM (SELECT t.geonameid, t.name, t.lat, t.lon, t.population, t.country_code,"
            " r.name AS region, c.name AS country, meters(t.lat, t.lon, :lat, :lon) AS distance_m"
            f" FROM towns t{_WHERE.format(t='t')} WHERE {TOWNS} AND {where})"
            " WHERE distance_m <= :within ORDER BY distance_m, population DESC, geonameid LIMIT 1",
            {**params, "within": float(within_m)}).fetchone()
        return dict(row) if row else None

    def landmark_at(self, lat: float, lon: float, sure_m: float = 0) -> dict[str, Any] | None:
        """The landmark a point is at: within its kind's reach (landmarks.py),
        for a point sure to within sure_m meters (none for one less sure than
        the reach), the nearest for its reach where there are several. With
        how far (distance_m) and its kind; None: at none."""
        where, params = band(lat, lon, FARTHEST_M, "l")
        row = self._db.execute(
            "SELECT * FROM (SELECT l.geonameid, l.name, l.lat, l.lon, 0 AS population, l.country_code,"
            " l.feature_code, r.name AS region, c.name AS country, meters(l.lat, l.lon, :lat, :lon) AS distance_m,"
            f" reach(l.feature_code) AS reach_m FROM landmarks l{_WHERE.format(t='l')} WHERE {where})"
            " WHERE distance_m <= reach_m AND reach_m >= :sure"
            " ORDER BY distance_m / reach_m, distance_m, geonameid LIMIT 1",
            {**params, "sure": float(sure_m)}).fetchone()
        return _kind(row) if row else None

    def get(self, geonameid: int) -> dict[str, Any] | None:
        """A town or a landmark by its GeoNames id, with its kind (None: a town)."""
        for table, t, code, population in (("towns", "t", "NULL", "t.population"),
                                           ("landmarks", "l", "l.feature_code", "0")):
            row = self._db.execute(
                f"SELECT {t}.geonameid, {t}.name, {t}.lat, {t}.lon, {population} AS population, {t}.country_code,"
                f" {code} AS feature_code, r.name AS region, c.name AS country"
                f" FROM {table} {t}{_WHERE.format(t=t)} WHERE {t}.geonameid = :g", {"g": geonameid}).fetchone()
            if row is not None:
                return _kind(row)
        return None


def version_of(path: str) -> int | None:
    """The data version of the list at path; None when there's none (or it can't be read)."""
    if not os.path.exists(path):
        return None
    try:
        with Places(path) as found:
            return int(found.meta().get("data_version", "0"))
    except (sqlite3.Error, ValueError):
        return None
