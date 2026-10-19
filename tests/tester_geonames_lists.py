"""GeoNames' files made up for the tester's parity tests (ADR-023 phase 3), in
GeoNames' own formats, with the edge cases on purpose: towns at the same
point with the same population (a full tie), sections of towns and
historical places (never named), every landmark kind at just inside and
just outside its reach, every_one kinds without a Wikipedia link, links
that aren't Wikipedia's, other names in several languages (English and
not, the same name twice, historic and colloquial ones, codes), names that
tie on tier and length, LIKE's own characters, accents and ASCII
spellings, both sides of the antimeridian and near the poles. Seeded:
the same files every time.

    files(seed) -> {file name: bytes}       the five files download.geonames.org serves
    points(seed) -> [(lat, lon, sure_m)]     where to ask, chosen around those places
    queries(seed) -> [str]                   what to search for
"""

from __future__ import annotations

import io
import math
import random
import zipfile

# Kinds (feature codes) and their reach, as GeoNames' landmarks.py says them today; the parity test holds the
# plug-in to the same answers, so it reads these only to place points just inside and just outside.
REACH = {"MNMT": 250, "TOWR": 250, "MUS": 250, "CH": 250, "OBS": 250, "GYSR": 250, "CSTL": 500, "PRK": 1500,
         "ZOO": 500, "MT": 1000, "VLC": 1000, "AMUS": 1500, "AIRP": 1500, "BCH": 1000}
EVERY_ONE = {"OBS", "ZOO", "VLC", "AMUS", "GYSR"}
NOT_A_TOWN = ("PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH")
COUNTRIES = [("TT", "Trinidad and Tobago"), ("FR", "France"), ("US", "United States"), ("CH", "Switzerland"),
             ("BR", "Brazil"), ("FJ", "Fiji"), ("RU", "Russia"), ("NO", "Norway"), ("AQ", "Antarctica"),
             ("JP", "Japan"), ("DE", "Germany")]
REGIONS = [("TT.05", "Port of Spain"), ("FR.11", "Île-de-France"), ("US.TX", "Texas"), ("US.FL", "Florida"),
           ("CH.ZH", "Zurich"), ("BR.27", "São Paulo"), ("FJ.01", "Central"), ("RU.15", "Chukotka"),
           ("NO.21", "Svalbard"), ("JP.40", "Tokyo"), ("DE.02", "Bayern"), ("FR.76", "Occitanie")]
STEMS = ["Port", "Saint", "San", "Santa", "New", "Old", "Mount", "Lake", "Zürich", "São", "München", "Kraków",
         "Łódź", "Straße", "Ærø", "Œuvre", "Hà Nội", "50% Town", "x_y", "back\\slash", "O'Neil", "Tour", "Champ",
         "Eiffel", "Magic", "Paris", "Pari", "Par", "Sampa", "Zurich", "Zuerich", "Ó", "Østby"]


def _place_line(gid, name, ascii_name, lat, lon, cls, code, cc, admin1, population=0):
    return "\t".join([str(gid), name, ascii_name, "", f"{lat:.5f}", f"{lon:.5f}", cls, code, cc, "", admin1, "", "",
                      "", str(population), "", "10", "Etc/UTC", "2026-01-01"])


def _ascii(name: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)) \
        .replace("ß", "ss").replace("Ø", "O").replace("ø", "o").replace("Ł", "L").replace("ł", "l") \
        .replace("Æ", "AE").replace("æ", "ae").replace("Œ", "OE").replace("œ", "oe")


def _offset(lat, lon, meters, bearing):
    d = meters / 6_371_000.0
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(bearing))
    lo2 = lo1 + math.atan2(math.sin(bearing) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), (math.degrees(lo2) + 540) % 360 - 180


class _Lists:
    def __init__(self, seed: int) -> None:
        rng = random.Random(seed)
        self.rng = rng
        self.towns, self.landmarks, self.names = [], [], []
        gid = 1_000_000
        centres = [(10.66, -61.52, "TT", "TT.05"), (48.85, 2.35, "FR", "FR.11"), (33.66, -95.56, "US", "US.TX"),
                   (28.42, -81.58, "US", "US.FL"), (47.37, 8.55, "CH", "CH.ZH"), (-23.55, -46.64, "BR", "BR.27"),
                   (-16.5, 179.95, "FJ", "FJ.01"), (65.7, -179.9, "RU", "RU.15"), (78.22, 15.65, "NO", "NO.21"),
                   (-77.85, 166.67, "AQ", "AQ.00"), (35.68, 139.69, "JP", "JP.40"), (48.14, 11.58, "DE", "DE.02")]
        for lat0, lon0, cc, region in centres:
            for _ in range(40):
                gid += 1
                stem = rng.choice(STEMS)
                name = stem if rng.random() < 0.3 else f"{stem} {rng.choice(['Beach', 'Hill', 'Vale', 'de Mars', 'City'])}"
                lat, lon = _offset(lat0, lon0, rng.uniform(0, 60_000), rng.uniform(0, 2 * math.pi))
                code = rng.choice(["PPL"] * 6 + ["PPLA", "PPLC", *NOT_A_TOWN])
                pop = rng.choice([1000, 1500, 20000, 50000, 2_000_000])
                self.towns.append((gid, name, _ascii(name), lat, lon, code, cc, region.split(".")[1], pop))
            # A full tie: two towns at one point with one population.
            twin_lat, twin_lon = _offset(lat0, lon0, 20_000, 0.7)
            for _ in range(2):
                gid += 1
                self.towns.append((gid, "Twin", "Twin", twin_lat, twin_lon, "PPL", cc, region.split(".")[1], 5000))
            for code in REACH:
                gid += 1
                name = f"{rng.choice(STEMS)} {code.title()}"
                lat, lon = _offset(lat0, lon0, rng.uniform(1000, 40_000), rng.uniform(0, 2 * math.pi))
                linked = code in EVERY_ONE and rng.random() < 0.5 or rng.random() < 0.8
                self.landmarks.append((gid, name, _ascii(name), lat, lon, code, cc, region.split(".")[1], linked))
        # Places at no real point (a device's 0, 0; out of range), which today's search returns and Lumiverb's
        # checks of a plug-in's place refuse (src/shared/location.py is_fix): the plug-in leaves them out.
        for name, lat, lon in (("Nullisland", 0.0, 0.0), ("Overthepole", 91.0, 10.0), ("Pasttheline", 10.0, 181.0)):
            gid += 1
            self.towns.append((gid, name, name, lat, lon, "PPL", "FR", "11", 3000))
        # Other names: languages, English twice, historic, colloquial, codes, the same as its own, ties.
        aid = 0
        for gid, name, *_ in [*self.towns, *self.landmarks]:
            for _ in range(rng.choice([0, 0, 1, 2, 3])):
                aid += 1
                lang = rng.choice(["en", "fr", "de", "it", "", "zh-Hant", "post", "iata", "link", "abbr", "en"])
                other = rng.choice([name, name.upper(), f"{rng.choice(STEMS)} {rng.choice(['Tower', 'Park', 'Burg'])}",
                                    "Eiffel Tower", "Tour Eiffel", "Magic Kingdom", "8001"])
                historic = "1" if rng.random() < 0.1 else ""
                colloquial = "1" if rng.random() < 0.1 else ""
                self.names.append((aid, gid, lang, other, historic, colloquial))
        for gid, *_rest, linked in self.landmarks:
            aid += 1
            url = f"https://en.wikipedia.org/wiki/L{gid}" if linked else f"https://example.com/l{gid}"
            self.names.append((aid, gid, "link", url, "", ""))

    def files(self) -> dict[str, bytes]:
        towns = "\n".join(_place_line(g, n, a, la, lo, "P", c, cc, r, p) for g, n, a, la, lo, c, cc, r, p in self.towns)
        marks = "\n".join(_place_line(g, n, a, la, lo, "S", c, cc, r) for g, n, a, la, lo, c, cc, r, _ in self.landmarks)
        names = "\n".join("\t".join([str(a), str(g), lang, n, "", "", col, his, "", ""])
                          for a, g, lang, n, his, col in self.names)
        out = {}
        for zname, member, body in (("cities1000.zip", "cities1000.txt", towns + "\nnot a line\n"),
                                    ("allCountries.zip", "allCountries.txt", marks + "\n" + towns + "\n"),
                                    ("alternateNamesV2.zip", "alternateNamesV2.txt", names + "\n")):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr(member, body)
            out[zname] = buf.getvalue()
        out["admin1CodesASCII.txt"] = "".join(f"{c}\t{n}\t{_ascii(n)}\t1\n" for c, n in REGIONS).encode()
        out["countryInfo.txt"] = ("#ISO\tISO3\tISO-Numeric\tfips\tCountry\n" + "".join(
            f"{iso}\tXXX\t000\t{iso}\t{name}\n" for iso, name in COUNTRIES)).encode()
        return out

    def points(self) -> list[tuple[float, float, float]]:
        rng = self.rng
        out = []
        for _g, _n, _a, lat, lon, *_ in self.towns:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0.0, 0.0):
                continue  # a place at no real point: Lumiverb hands a plug-in no such location
            out.append((lat, lon, 0.0))
            out.append((*_offset(lat, lon, rng.uniform(100, 30_000), rng.uniform(0, 2 * math.pi)), 0.0))
        for _g, _n, _a, lat, lon, code, *_ in self.landmarks:
            reach = REACH[code]
            for meters, sure in ((reach * 0.98, 0.0), (reach * 1.02, 0.0), (reach * 0.5, reach * 1.0),
                                 (reach * 0.5, reach * 1.1), (0.0, 0.0)):
                out.append((*_offset(lat, lon, meters, rng.uniform(0, 2 * math.pi)), sure))
        out += [(0.0, 179.99, 0.0), (0.0, -179.99, 0.0), (-16.5, -179.99, 0.0), (65.7, 179.99, 0.0),
                (89.99, 0.0, 0.0), (-89.99, 0.0, 0.0)]
        return [(round(a, 6), round(b, 6), c) for a, b, c in out]

    def queries(self) -> list[str]:
        rng = self.rng
        out = []
        for _g, name, *_ in rng.sample(self.towns, 80) + rng.sample(self.landmarks, 40):
            out.append(name[: rng.randint(1, len(name))])
        for _a, _g, lang, name, *_ in rng.sample(self.names, 60):
            if lang not in ("link",):
                out.append(name[: rng.randint(1, len(name))])
        out += ["Twin", "Paris", "Pari", "Par", "zurich", "Zürich", "Zuerich", "Eiffel Tower", "Tour Eiffel",
                "Magic", "50%", "x_y", "back\\", "%", "_", "\\", "O'", "Saint, Île", "San, BR", "San, br",
                "Port, Trinidad", "New, Texas", "Twin, FJ", ",", "  Paris  ", "Ærø", "aero", "Straße", "strasse",
                "Łódź", "lodz", "Hà Nội", "ha noi", "Østby", "8001", "Twin, xx", "Nullisland", "Overthepole",
                "Pasttheline"]
        return out


def lists(seed: int = 20261019) -> _Lists:
    return _Lists(seed)
