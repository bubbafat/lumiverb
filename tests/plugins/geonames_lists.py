"""GeoNames' five files, written small for the GeoNames plug-in's tests
(src/own_plugins/geonames/), with a local server standing in for
download.geonames.org, and the questions the parity tests ask of today's
Place names and of the plug-in alike.

The list is made for its edges: two towns at one point with one population
(a full tie), two equally near with different populations, sections of a
town and historical places beside a real one, towns either side of the
antimeridian and near the poles, a landmark of each reach with points just
inside and just outside it, two landmarks tied for nearness, kinds kept
whole and not, links that aren't Wikipedia's, other names in several
languages (one twice, English kept), colloquial and historic names, codes
that aren't names, ASCII spellings, LIKE's own characters, and a doubled
space in a name. ODD adds what Lumiverb won't show (control characters, <
and >, a name longer than 200 characters, points that aren't real), which
only the plug-in is asked about: today's Place names keeps them as they are.
"""

from __future__ import annotations

import http.server
import io
import math
import os
import shutil
import threading
import zipfile
from functools import partial
from pathlib import Path
from typing import Any

EARTH_M = 6_371_000.0
MB = 1024 * 1024


def town(gid: int, name: str, ascii_name: str, lat: float, lon: float, cc: str, admin1: str, population: int,
         code: str = "PPL") -> str:
    return "\t".join([str(gid), name, ascii_name, "", repr(lat), repr(lon), "P", code, cc, "", admin1, "", "", "",
                      str(population), "", "10", "Etc/UTC", "2026-01-01"])


def feature(gid: int, name: str, lat: float, lon: float, cls: str, code: str, cc: str, admin1: str,
            ascii_name: str | None = None) -> str:
    return "\t".join([str(gid), name, name if ascii_name is None else ascii_name, "", repr(lat), repr(lon), cls,
                      code, cc, "", admin1, "", "", "", "0", "", "30", "Etc/UTC", "2026-01-01"])


def alt(row: int, gid: int, language: str, name: str, *, colloquial: bool = False, historic: bool = False) -> str:
    return "\t".join([str(row), str(gid), language, name, "", "", "1" if colloquial else "", "1" if historic else "",
                      "", ""])


def north(lat: float, lon: float, meters: float) -> tuple[float, float]:
    """The point that many meters north (south, negative): along a meridian, whose distance is the arc."""
    return lat + math.degrees(meters / EARTH_M), lon


PORT_OF_SPAIN = (10.66668, -61.51889)
PARIS = (48.85341, 2.3488)
ZURICH = (47.36667, 8.55)
TWINS = (45.0, 10.0)
EVEN = (46.0, 10.0)  # Bigtown and Smalltown as far north and south of it
FULL_TIE = {9100001, 9100002}  # Twinton twice: Postgres names either, the plug-in the lowest id
EIFFEL = (48.8583, 2.2945)
CASTLE = (51.0, -1.0)
MOUNTAIN = (52.0, -1.5)
MAGIC_KINGDOM = (28.4187, -81.5812)
PAIR = (40.0, 20.0)  # two monuments at one point
RATIO = (41.0, 20.0)  # a monument 200 m north, a park 300 m south: the park is "at" (0.2 of its reach)

TOWNS = [
    town(3573890, "Port of Spain", "Port of Spain", *PORT_OF_SPAIN, "TT", "05", 49031, "PPLC"),
    town(3573740, "San Juan", "San Juan", 10.65, -61.4, "TT", "05", 54000),
    town(2988507, "Paris", "Paris", *PARIS, "FR", "11", 2138551, "PPLC"),
    town(4717560, "Paris", "Paris", 33.66094, -95.55551, "US", "TX", 24171),
    town(2657896, "Zürich", "Zuerich", *ZURICH, "CH", "ZH", 415367, "PPLA"),  # GeoNames' own spellings
    town(2657897, "Zürich (Kreis 7)", "Zuerich (Kreis 7)", 47.37, 8.56, "CH", "ZH", 37000, "PPLX"),  # a section
    town(3448439, "São Paulo", "Sao Paulo", -23.5475, -46.63611, "BR", "27", 10021295, "PPLA"),
    town(9999991, "Parisot", "Parisot", 44.26, 1.85, "FR", "76", 1100),
    town(2294768, "Sampa", "Sampa", 8.0, -2.7, "GH", "04", 20000),  # São Paulo is "Sampa" too
    town(9100001, "Twinton", "Twinton", *TWINS, "IT", "25", 5000),
    town(9100002, "Twinton", "Twinton", *TWINS, "IT", "25", 5000),  # a full tie: the lowest id, in the plug-in
    # 1/32° either side of EVEN, which a double holds exactly: as far, to the last bit, so the population decides.
    town(9100003, "Bigtown", "Bigtown", EVEN[0] + 0.03125, EVEN[1], "IT", "25", 9000),
    town(9100004, "Smalltown", "Smalltown", EVEN[0] - 0.03125, EVEN[1], "IT", "25", 1000),
    town(9100005, "Oldtown", "Oldtown", *north(*EVEN, 10), "IT", "25", 50000, "PPLH"),  # historical: never named
    town(9100006, "Lostville", "Lostville", *north(*EVEN, 20), "IT", "25", 50000, "PPLQ"),  # abandoned
    town(9100007, "Ruinburg", "Ruinburg", *north(*EVEN, 30), "IT", "25", 50000, "PPLW"),  # destroyed
    town(9100008, "Exton", "Exton", *north(*EVEN, 40), "IT", "25", 50000, "PPLCH"),  # a historical capital
    town(9100010, "Eastside", "Eastside", -16.5, 179.99, "FJ", "01", 3000),
    town(9100011, "Westside", "Westside", -16.5, -179.99, "FJ", "01", 2000),
    town(9100012, "Polar Station", "Polar Station", 89.9, 0.0, "AQ", "", 1000),
    town(9100013, "South Station", "South Station", -89.9, 45.0, "AQ", "", 1000),
    town(9100014, "100% Town", "100% Town", 30.0, 30.0, "EG", "11", 1500),
    town(9100015, "Under_score", "Under_score", 30.1, 30.1, "EG", "11", 1400),
    town(9100016, "Back\\slash", "Back\\slash", 30.2, 30.2, "EG", "11", 1300),
    town(9100017, "Tiedtown", "Tiedtown", 31.0, 31.0, "EG", "11", 1200),
    "not a place line",
]
REGIONS = "\n".join([
    "TT.05\tPort of Spain\tPort of Spain\t3573891", "FR.11\tÎle-de-France\tIle-de-France\t3012874",
    "US.TX\tTexas\tTexas\t4736286", "CH.ZH\tZurich\tZurich\t2657895", "FR.76\tOccitanie\tOccitanie\t11071623",
    "BR.27\tSão Paulo\tSao Paulo\t3448433", "US.FL\tFlorida\tFlorida\t4155751", "IT.25\tLombardia\tLombardia\t3174618",
    "FJ.01\tCentral\tCentral\t2205218", "EG.11\tCairo\tCairo\t360631", "GB.ENG\tEngland\tEngland\t6269131",
]) + "\n"
COUNTRIES = "\n".join([
    "#ISO\tISO3\tISO-Numeric\tfips\tCountry", "TT\tTTO\t780\tTD\tTrinidad and Tobago", "FR\tFRA\t250\tFR\tFrance",
    "US\tUSA\t840\tUS\tUnited States", "CH\tCHE\t756\tSZ\tSwitzerland", "BR\tBRA\t076\tBR\tBrazil",
    "IT\tITA\t380\tIT\tItaly", "FJ\tFJI\t242\tFJ\tFiji", "AQ\tATA\t010\tAY\tAntarctica", "EG\tEGY\t818\tEG\tEgypt",
    "GH\tGHA\t288\tGH\tGhana", "GB\tGBR\t826\tUK\tUnited Kingdom",
]) + "\n"
FEATURES = [
    feature(4164161, "Magic Kingdom, Walt Disney World", *MAGIC_KINGDOM, "L", "AMUS", "US", "FL"),  # kept whole
    feature(6254976, "Tour Eiffel", *EIFFEL, "S", "MNMT", "FR", "11"),  # a Wikipedia article
    feature(6618997, "Champ de Mars", 48.8561, 2.2980, "L", "PRK", "FR", "11"),  # one too
    feature(9200001, "Castle  Rock", *CASTLE, "S", "CSTL", "GB", "ENG"),  # a doubled space, as GeoNames has some
    feature(9200002, "Mount Test", *MOUNTAIN, "T", "MT", "GB", "ENG"),
    feature(9200003, "Pair Monument A", *PAIR, "S", "MNMT", "IT", "25"),
    feature(9200004, "Pair Monument B", *PAIR, "S", "MNMT", "IT", "25"),  # at the same point: the lowest id
    feature(9200005, "Ratio Monument", *north(*RATIO, 200), "S", "MNMT", "IT", "25"),
    feature(9200006, "Ratio Park", *north(*RATIO, -300), "L", "PRK", "IT", "25"),
    feature(9200007, "Star Watch", 33.0, 33.0, "S", "OBS", "EG", "11"),  # kept whole, no article
    feature(9200008, "Lion Zoo", 33.1, 33.1, "S", "ZOO", "EG", "11"),  # kept whole
    feature(9999994, "Eiffel Peak", 46.3, -122.4, "T", "PK", "US", "WA"),  # an article, no other names
    feature(9999992, "Hôtel du Coin", 48.8584, 2.2946, "S", "HTL", "FR", "11"),  # a kind kept, no article
    feature(9999993, "Ruisseau", 48.86, 2.30, "H", "STM", "FR", "11"),  # an article, not a kind kept
    feature(9999995, "Linked Elsewhere", 48.87, 2.31, "S", "MUS", "FR", "11"),  # a link, not Wikipedia's
    feature(2988507, "Paris", *PARIS, "P", "PPLC", "FR", "11"),  # a town: the towns' list's
    "not a feature line",
]
_LINKED = (6254976, 6618997, 9200001, 9200002, 9200003, 9200004, 9200005, 9200006, 9999994, 9999993)
NAMES = "\n".join([
    alt(1, 6254976, "fr", "Eiffel Tower"),  # the same name as its English one, listed first
    alt(2, 6254976, "en", "Eiffel Tower"),
    alt(3, 6254976, "de", "Eiffelturm"),
    *(f"{100 + n}\t{gid}\tlink\thttps://en.wikipedia.org/wiki/{gid}\t\t\t\t\t\t" for n, gid in enumerate(_LINKED)),
    "200\t9999992\tlink\thttps://example.com/hotel\t\t\t\t\t\t",  # a link, not Wikipedia's
    "201\t9999995\tlink\thttps://example.com/museum\t\t\t\t\t\t",
    alt(4, 2657896, "it", "Zurigo"),
    alt(5, 2657896, "de", "Zürich"),  # its own name, folded: not again
    alt(6, 2657896, "la", "Turicum", historic=True),
    alt(7, 2657896, "en", "Zurchy", colloquial=True),
    "8\t2657896\tpost\t8001\t\t\t\t\t\t",  # a code, not a name
    "9\t2657896\tiata\tZRH\t\t\t\t\t\t",
    alt(10, 3448439, "", "Sampa"),  # no language: a name all the same
    alt(11, 9999993, "fr", "Le Ruisseau"),  # not a place kept
    alt(12, 9200001, "en", "Rock Castle"),
    alt(13, 9200001, "de", "Felsenburg"),
    alt(14, 9200002, "fr", "Mont Test"),
    alt(15, 9100017, "de", "Tiedorf"),  # two names of a place, one tier, one length: by their characters
    alt(16, 9100017, "nl", "Tiedorp"),
    alt(17, 2988507, "en", "Paris"),  # its own name again: not kept
    alt(18, 2988507, "la", "Lutetia", historic=True),
    alt(19, 2988507, "eo", "Parizo"),
    alt(20, 4164161, "es", "Reino Mágico"),
    "not a name line",
]) + "\n"

# What Lumiverb won't show, and points that aren't real: the plug-in leaves them out or cleans them.
LONG = " ".join(["Llanfairpwllgwyngyll", "and", "the", "very", "long", "name", "of", "a", "town"] * 6)  # > 200
ODD_TOWNS = [
    town(9300001, "Bad\x07Bell<b>town</b>", "Badbelltown", 35.0, 35.0, "EG", "11", 1000),
    town(9300002, "Nowhere", "Nowhere", 0.0, 0.0, "EG", "11", 1000),  # no real point
    town(9300003, "Offworld", "Offworld", 95.0, 35.0, "EG", "11", 1000),  # out of range
    town(9300004, "<>", "<>", 35.1, 35.1, "EG", "11", 1000),  # no name left
    town(9300005, LONG, LONG, 35.2, 35.2, "EG", "11", 1000),
]
ODD_REGIONS = "XX.12\tGiza<script>\tGiza\t360997\n"
ODD_COUNTRIES = "XX\tXXX\t000\tXX\tLong\x1fland\n"
ODD_FEATURES = [feature(9300006, "Tower <i>of</i> Tags", 35.3, 35.3, "S", "OBS", "XX", "12")]  # kept whole
ODD_NAMES = alt(30, 9300001, "en", "Bell\x00town Again") + "\n" + alt(31, 9300005, "en", LONG + " too") + "\n"


def write(folder: Path, *, odd: bool = False, stored_mb: int = 0, filler_mb: int = 0) -> Path:
    """The five files into folder (made); odd: with what Lumiverb won't show. allCountries.zip can be made
    bigger with lines that aren't features: stored_mb of them uncompressed (a download that takes a while),
    filler_mb compressed (a build that reads for a while)."""
    folder.mkdir(parents=True, exist_ok=True)
    towns, features, names = TOWNS + (ODD_TOWNS if odd else []), FEATURES + (ODD_FEATURES if odd else []), NAMES
    regions, countries = REGIONS + (ODD_REGIONS if odd else ""), COUNTRIES + (ODD_COUNTRIES if odd else "")
    if odd:
        names += ODD_NAMES
    filler = "not a feature, only filler\n" * ((stored_mb + filler_mb) * MB // 27)
    for name, member, text in (("cities1000.zip", "cities1000.txt", "\n".join(towns) + "\n"),
                               ("allCountries.zip", "allCountries.txt", "\n".join(features) + "\n" + filler),
                               ("alternateNamesV2.zip", "alternateNamesV2.txt", names)):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED if stored_mb else zipfile.ZIP_DEFLATED) as z:
            z.writestr(member, text)
        (folder / name).write_bytes(buf.getvalue())
    (folder / "admin1CodesASCII.txt").write_text(regions, encoding="utf-8")
    (folder / "countryInfo.txt").write_text(countries, encoding="utf-8")
    return folder


class Mirror:
    """A local stand-in for download.geonames.org serving a folder: every request it was sent, by method and
    file (asked); a file can be held part way (hold: its first bytes sent, then nothing until let go), or said
    to be bigger than it is when it's asked about (claimed)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.asked: list[tuple[str, str]] = []
        self.held: dict[str, int] = {}
        self.claimed: dict[str, int] = {}
        self.let_go = threading.Event()
        mirror = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def send_head(self):  # noqa: ANN202 — SimpleHTTPRequestHandler's
                mirror.asked.append((self.command, self.path.lstrip("/")))
                return super().send_head()

            def send_header(self, keyword: str, value: str) -> None:
                name = self.path.lstrip("/")
                if self.command == "HEAD" and keyword == "Content-Length" and name in mirror.claimed:
                    value = str(mirror.claimed[name])
                super().send_header(keyword, value)

            def copyfile(self, source, outputfile) -> None:  # noqa: ANN001 — SimpleHTTPRequestHandler's
                name = os.path.basename(getattr(source, "name", ""))
                if name not in mirror.held:
                    super().copyfile(source, outputfile)
                    return
                outputfile.write(source.read(mirror.held[name]))
                outputfile.flush()
                mirror.let_go.wait(120)
                shutil.copyfileobj(source, outputfile)  # the rest, to whoever still asks

        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # a plug-in stopped mid-download: its connection is gone

        handler = partial(Handler, directory=str(folder))
        self.server = Server(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def gets(self, name: str) -> int:
        return sum(1 for method, asked in self.asked if method == "GET" and asked == name)

    def hold(self, name: str, after: int) -> None:
        self.held[name] = after
        self.let_go.clear()

    def release(self) -> None:
        self.held.clear()
        self.let_go.set()

    def close(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# The questions: asked of today's Place names and of the plug-in alike
# ---------------------------------------------------------------------------

REACHES = {"MNMT": 250, "CSTL": 500, "MT": 1000, "AMUS": 1500}
_LANDMARKS = {"MNMT": EIFFEL, "CSTL": CASTLE, "MT": MOUNTAIN, "AMUS": MAGIC_KINGDOM}


def questions() -> dict[str, list]:
    """Every question, by what's asked: nearest [lat, lon, within_m], landmark_at [lat, lon, sure_m], search
    [q, limit], get [geonameid], facts [lat, lon, sure_m, radius_km]. A point a measured distance from another
    is along a meridian, its distance known to well under a meter either way."""
    nearest: list[list[float]] = []
    for lat, lon in (PORT_OF_SPAIN, PARIS, ZURICH, (-23.5475, -46.63611), (33.66094, -95.55551)):
        for within_m in (1000.0, 25000.0):
            nearest.append([lat, lon, within_m])
    for d in (5000.0, 12000.0):  # a town at a known distance: just within it, and just past it
        lat, lon = north(*PARIS, d)
        nearest += [[lat, lon, d + 0.5], [lat, lon, d - 0.5]]
    nearest += [
        [10.66, -61.46, 25000.0],  # between Port of Spain and San Juan
        [*TWINS, 1000.0],  # two towns at one point, one population: the lowest id in the plug-in
        [*EVEN, 5000.0],  # Bigtown and Smalltown as near: the most populous
        [*north(*EVEN, 25), 5000.0],  # beside the historical places: none of them named
        [47.3701, 8.5601, 5000.0],  # beside a section of Zürich: Zürich
        [-16.5, 179.995, 5000.0], [-16.5, -179.999, 5000.0], [-16.5, 179.9, 2000.0],  # the antimeridian
        [89.95, 120.0, 20000.0], [89.99, -170.0, 20000.0], [-89.95, -100.0, 20000.0],  # near the poles
        [0.0, -150.0, 200000.0],  # an ocean: none
    ]
    for lat in range(-60, 61, 30):  # a coarse grid, wide radii
        for lon in range(-120, 121, 60):
            nearest += [[float(lat), float(lon), 200000.0], [float(lat) + 0.5, float(lon) + 0.5, 25000.0]]

    landmark_at: list[list[float]] = []
    for code, (lat, lon) in _LANDMARKS.items():
        reach = REACHES[code]
        landmark_at += [[lat, lon, 0.0], [lat, lon, reach - 1.0], [lat, lon, float(reach)], [lat, lon, reach + 1.0]]
        landmark_at += [[*north(lat, lon, reach - 1.0), 0.0], [*north(lat, lon, reach + 1.0), 0.0]]
        landmark_at += [[*north(lat, lon, -(reach - 1.0)), 0.0]]
    landmark_at += [[*PAIR, 0.0], [*north(*PAIR, 100), 0.0], [*RATIO, 0.0], [*RATIO, 260.0], [33.0, 33.0, 0.0],
                    [33.1, 33.1, 0.0], [48.8584, 2.2946, 0.0], [48.86, 2.30, 0.0], [48.87, 2.31, 0.0],
                    [0.0, -150.0, 0.0]]

    search = [[q, 10] for q in (
        "paris", "Paris", "PAR", "par", "paris, tex", "paris, us", "paris, ile", "paris, fr", "paris, nowhere",
        "zurich", "Zürich", "ZUR", "zuerich", "zurig", "zurigo", "zurigo, brazil", "zurigo, switz", "turicum",
        "zurchy", "8001", "zrh", "sao paulo", "São", "sampa", "sam", "port of", "Port of Spain", "san",
        "magic", "reino", "eiffel", "eiffelt", "eiffel tower", "tour eif", "champ", "champ, france", "castle",
        "rock", "felsen", "mount", "mont", "twin", "big", "small", "old", "lost", "exton", "east", "west", "polar",
        "south", "100", "100%", "%", "_", "\\", "under_", "under%", "back\\", "tied", "tiedo", "tiedorf", "pair",
        "ratio", "star", "lion", "hôtel du coin", "ruisseau", "le ruisseau", "linked", "lutetia", "pariz", "a",
        "e", "t", " ", ",", "paris,", ", france", "x" * 50)]
    search += [["par", 1], ["par", 2], ["t", 3], ["e", 25]]
    get = [3573890, 2988507, 2657896, 2657897, 4164161, 6254976, 9200004, 9999992, 1, 9100005]
    facts = [
        [*EIFFEL, 0.0, 25],  # a landmark and a town near: "At Tour Eiffel", "Near Paris, …"
        [*MAGIC_KINGDOM, 0.0, 25],  # a landmark, no town near: "At" its label, which says where
        [*PARIS, 0.0, 25],  # a town, at no landmark
        [0.0, -150.0, 0.0, 25],  # neither
        [*EIFFEL, 300.0, 25],  # too unsure to be at the tower: the town alone
        [*north(*PARIS, 12000), 0.0, 10],  # the town too far for its radius
        [*north(*PARIS, 12000), 0.0, 13],
        [*CASTLE, 0.0, 200], [*PAIR, 0.0, 1], [*RATIO, 0.0, 1],
    ]
    return {"nearest": nearest, "landmark_at": landmark_at, "search": search, "get": get, "facts": facts}


def at_and_near(town: dict[str, Any] | None, mark: dict[str, Any] | None, label: Any) -> dict[str, str | None]:
    """What the Lightbox has said of a clip from its nearest town and its landmark (LocationRow.tsx): "At" the
    landmark's name when a town is near, else its label (which says where); "Near" the town's label."""
    return {"at": (mark["name"] if town else label(mark)) if mark else None, "near": label(town) if town else None}


_KEYS = {"nearest": ("geonameid", "name", "region", "country", "population", "distance_m"),
         "landmark_at": ("geonameid", "name", "region", "country", "kind", "distance_m"),
         "search": ("geonameid", "name", "region", "country", "kind", "matched", "lat", "lon", "population"),
         "get": ("geonameid", "name", "region", "country", "kind", "lat", "lon", "population")}


def answers(store: Any, label: Any) -> dict[str, list]:
    """Every question's answer from a store of places with today's Place names' methods (nearest, landmark_at,
    search, get: repository/places.py's PlaceRepository, or the plug-in's places.Places), by what's asked."""

    def kept(what: str, row: Any) -> dict[str, Any] | None:
        return None if row is None else {k: row.get(k) for k in _KEYS[what]}

    q = questions()
    return {
        "nearest": [kept("nearest", store.nearest(*a)) for a in q["nearest"]],
        "landmark_at": [kept("landmark_at", store.landmark_at(*a)) for a in q["landmark_at"]],
        "search": [[kept("search", r) for r in store.search(*a)] for a in q["search"]],
        "get": [kept("get", store.get(a)) for a in q["get"]],
        "facts": [at_and_near(store.nearest(lat, lon, radius_km * 1000), store.landmark_at(lat, lon, sure), label)
                  for lat, lon, sure, radius_km in q["facts"]],
    }


def _same(what: str, today: Any, theirs: Any) -> bool:
    if today is None or theirs is None:
        return today is theirs
    if what == "search":
        return len(today) == len(theirs) and all(_same("get", a, b) for a, b in zip(today, theirs))
    if what == "facts":
        return today == theirs
    a, b = dict(today), dict(theirs)
    if what == "nearest" and a["geonameid"] in FULL_TIE and b["geonameid"] in FULL_TIE:
        a["geonameid"] = b["geonameid"]  # a full tie: Postgres names either (the plug-in, the lowest id)
    if "distance_m" in a and abs(a.pop("distance_m") - b.pop("distance_m")) > 1:
        return False  # both double-precision haversine, but a platform's libm can differ in the last bit
    return a == b


def differences(today: dict[str, list], theirs: dict[str, list]) -> list[str]:
    """Each answer of theirs that isn't today's (answers()): ids, order, names, matched, kinds, populations
    exactly; distances within 1 m; a full tie (FULL_TIE) either way."""
    q = questions()
    out = [f"{what}: {len(today[what])} answers, {len(theirs[what])} of theirs" for what in q
           if len(today[what]) != len(theirs[what])]
    for what in q:
        for asked, a, b in zip(q[what], today[what], theirs[what]):
            if not _same(what, a, b):
                out.append(f"{what} {asked}: today {a}, theirs {b}")
    return out
