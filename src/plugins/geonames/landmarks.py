"""Which of GeoNames' features are landmarks (ADR-021's Place names, Robert
2026-10-18: "Do the landmarks as well"), and how near a location is "at" one.

GeoNames lists 13 million features besides towns; most are no one's
landmark (a creek, a farm, every hotel). A feature is kept when it's one of
these kinds and has a Wikipedia article (GeoNames' alternate names list its
links): 2026-10-18's lists keep about 70,000 that way, Paris's Eiffel Tower,
Disney's parks and Old Faithful among them. A few kinds are kept whole, being
few and each of them somewhere people go (GeoNames links only 75 of its 669
amusement parks to Wikipedia).

A landmark is a point, never an area, so each kind says how near its point a
location is at it (reach_m): a building's 250 m, a park's 1.5 km. A location
less sure than that (a guess "about 2 km") is at none of them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    noun: str  # GeoNames' name for it, said beside a landmark found by name: "amusement park"
    reach_m: int
    every_one: bool = False  # kept with or without a Wikipedia article


_BUILDING, _SITE, _NATURE, _GROUNDS = 250, 500, 1000, 1500

# GeoNames' feature codes (unique across its classes).
KINDS: dict[str, Kind] = {
    # A building, a monument, a spring: at it when within 250 m of its point.
    "MNMT": Kind("monument", _BUILDING), "TOWR": Kind("tower", _BUILDING),
    "LTHSE": Kind("lighthouse", _BUILDING), "OBS": Kind("observatory", _BUILDING, every_one=True),
    "PGDA": Kind("pagoda", _BUILDING), "SHRN": Kind("shrine", _BUILDING), "TMPL": Kind("temple", _BUILDING),
    "MSQE": Kind("mosque", _BUILDING), "CH": Kind("church", _BUILDING), "BLDG": Kind("building", _BUILDING),
    "HTL": Kind("hotel", _BUILDING), "CSNO": Kind("casino", _BUILDING), "THTR": Kind("theater", _BUILDING),
    "OPRA": Kind("opera house", _BUILDING, every_one=True), "MUS": Kind("museum", _BUILDING),
    "ARCH": Kind("arch", _BUILDING), "PYR": Kind("pyramid", _BUILDING, every_one=True),
    "AMTH": Kind("amphitheater", _BUILDING), "OBPT": Kind("observation point", _BUILDING),
    "GYSR": Kind("geyser", _BUILDING, every_one=True), "SPNG": Kind("spring", _BUILDING),
    "SPNT": Kind("hot spring", _BUILDING), "FLLS": Kind("waterfall", _BUILDING), "OVF": Kind("waterfall", _BUILDING),
    # A site with grounds: 500 m.
    "CSTL": Kind("castle", _SITE), "PAL": Kind("palace", _SITE), "FT": Kind("fort", _SITE),
    "HSTS": Kind("historical site", _SITE), "ANS": Kind("archaeological site", _SITE),
    "RUIN": Kind("ruins", _SITE), "MSTY": Kind("monastery", _SITE), "SQR": Kind("square", _SITE),
    "GDN": Kind("garden", _SITE), "MKT": Kind("market", _SITE), "STDM": Kind("stadium", _SITE),
    "ZOO": Kind("zoo", _SITE, every_one=True), "UNIV": Kind("university", _SITE),
    "CMTY": Kind("cemetery", _SITE), "DAM": Kind("dam", _SITE), "BDG": Kind("bridge", _SITE),
    "EST": Kind("estate", _SITE), "CAVE": Kind("cave", _SITE),
    # A mountain, a canyon, a beach: 1 km.
    "MT": Kind("mountain", _NATURE), "PK": Kind("peak", _NATURE), "VLC": Kind("volcano", _NATURE, every_one=True),
    "CNYN": Kind("canyon", _NATURE), "GRGE": Kind("gorge", _NATURE), "CLF": Kind("cliff", _NATURE),
    "BCH": Kind("beach", _NATURE), "CAPE": Kind("cape", _NATURE), "PASS": Kind("pass", _NATURE),
    "RK": Kind("rock", _NATURE), "BUTE": Kind("butte", _NATURE), "MESA": Kind("mesa", _NATURE),
    "CRTR": Kind("crater", _NATURE), "DUNE": Kind("dune", _NATURE), "WALL": Kind("wall", _NATURE),
    "PYRS": Kind("pyramids", _NATURE, every_one=True),
    # Grounds: a park, an amusement park, an airport: 1.5 km.
    "AMUS": Kind("amusement park", _GROUNDS, every_one=True), "PRK": Kind("park", _GROUNDS),
    "AIRP": Kind("airport", _GROUNDS), "RSRT": Kind("resort", _GROUNDS),
    "CTRS": Kind("space center", _GROUNDS, every_one=True), "GLCR": Kind("glacier", _GROUNDS),
    "RESN": Kind("nature reserve", _GROUNDS), "RES": Kind("reserve", _GROUNDS),
}

FARTHEST_M = max(k.reach_m for k in KINDS.values())


def reach_sql(column: str = "l.feature_code") -> str:
    """SQL: the reach in meters of a landmark by its feature code (the codes and numbers are these, constants)."""
    whens = " ".join(f"WHEN '{code}' THEN {kind.reach_m}" for code, kind in KINDS.items())
    return f"(CASE {column} {whens} ELSE 0 END)"
