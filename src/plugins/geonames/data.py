"""The place list (ADR-021's Place names): GeoNames' towns of 1,000 people
or more, their regions and countries, and its landmarks (landmarks.py: the
kinds kept, each with a Wikipedia article, some kinds every one), fetched
from download.geonames.org (or the mirror LUMIVERB_GEONAMES_URL names) and
kept in the control database, the same for every account. Only what's used
is kept: names, points, kind, region, country, population and time zone.

The big files (every feature, about 400 MB zipped; the alternate names, for
which have a Wikipedia article, about 200 MB) are downloaded to disk and
read as they're unzipped, never whole into memory, then deleted.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from src.plugins.geonames.landmarks import KINDS

PLACES, REGIONS, COUNTRIES = "cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt"
FEATURES, NAMES = "allCountries.zip", "alternateNamesV2.zip"
_MEMBERS = {PLACES: "cities1000.txt", FEATURES: "allCountries.txt", NAMES: "alternateNamesV2.txt"}
_MB = 1024 * 1024
# What each may be, at most, downloaded and unzipped, before it's refused. GeoNames' (2026-10-18):
# 11 MB and 32 MB; 403 MB and about 1.6 GB; 195 MB and about 0.7 GB; the two lists well under 1 MB.
_MOST = {PLACES: (100 * _MB, 300 * _MB), FEATURES: (1500 * _MB, 6000 * _MB), NAMES: (1000 * _MB, 4000 * _MB),
         REGIONS: (20 * _MB, None), COUNTRIES: (20 * _MB, None)}

Progress = Callable[..., None]


def parse_places(text: io.TextIOBase | Iterator[str]) -> Iterator[dict[str, Any]]:
    """GeoNames' cities file: tab-separated, 19 columns a place. A line that
    isn't one is skipped."""
    for line in text:
        p = line.rstrip("\n").split("\t")
        if len(p) < 19 or not p[0].isdigit():
            continue
        try:
            yield {"geonameid": int(p[0]), "name": p[1], "ascii_name": p[2] or p[1], "lat": float(p[4]),
                   "lon": float(p[5]), "feature_code": p[7], "country_code": p[8], "admin1_code": p[10],
                   "population": int(p[14] or 0), "timezone": p[17]}
        except ValueError:
            continue


def parse_regions(text: str) -> Iterator[dict[str, str]]:
    """admin1CodesASCII.txt: "TT.05<tab>Port of Spain<tab>Port of Spain<tab>3573890"."""
    for line in text.splitlines():
        p = line.split("\t")
        if len(p) >= 2 and "." in p[0] and p[1]:
            yield {"code": p[0], "name": p[1]}


def parse_countries(text: str) -> Iterator[dict[str, str]]:
    """countryInfo.txt: comments start with #; then ISO code first, the country's name fifth."""
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        p = line.split("\t")
        if len(p) >= 5 and len(p[0]) == 2 and p[4]:
            yield {"iso": p[0], "name": p[4]}


def wikipedia_linked(text: io.TextIOBase | Iterator[str]) -> set[int]:
    """The features with a Wikipedia article: alternateNamesV2.txt's "link"
    rows to wikipedia.org (id, geonameid, "link", the URL, ...)."""
    linked: set[int] = set()
    for line in text:
        if "\tlink\t" not in line or "wikipedia.org" not in line:
            continue
        p = line.split("\t", 4)
        if len(p) > 3 and p[2] == "link" and "wikipedia.org/" in p[3] and p[1].isdigit():
            linked.add(int(p[1]))
    return linked


def parse_landmarks(text: io.TextIOBase | Iterator[str], linked: set[int]) -> Iterator[dict[str, Any]]:
    """allCountries.txt's landmarks: a feature of a kind kept (landmarks.py)
    with a Wikipedia article, or of a kind kept whole. Its columns are the
    cities file's; a line that isn't one is skipped."""
    for line in text:
        p = line.split("\t", 11)
        if len(p) < 12 or p[7] not in KINDS or not p[0].isdigit():
            continue
        geonameid = int(p[0])
        if not KINDS[p[7]].every_one and geonameid not in linked:
            continue
        try:
            yield {"geonameid": geonameid, "name": p[1], "ascii_name": p[2] or p[1], "lat": float(p[4]),
                   "lon": float(p[5]), "feature_code": p[7], "country_code": p[8], "admin1_code": p[10]}
        except ValueError:
            continue


def _download(client: httpx.Client, url: str, dest: Path, most: int, progress: Progress, done: int,
              total: int | None) -> int:
    """The file at url into dest, refused past most bytes. Returns its size."""
    size = 0
    with client.stream("GET", url) as r, dest.open("wb") as out:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > most:
                raise ValueError(f"{url} is bigger than {most // _MB} MB")
            out.write(chunk)
            progress(done + size, total, "Downloading the place list")
    return size


def _member(folder: Path, name: str) -> Iterator[str]:
    """The lines of a downloaded zip's file, as they're unzipped (refused past its most)."""
    with zipfile.ZipFile(folder / name) as z:
        info = z.getinfo(_MEMBERS[name])
        most = _MOST[name][1]
        if most is not None and info.file_size > most:
            raise ValueError(f"{_MEMBERS[name]} is bigger than {most // _MB} MB")
        with z.open(info) as raw:
            yield from io.TextIOWrapper(raw, encoding="utf-8")


def folder() -> Path:
    """Where the files are downloaded to, while they're read: the data folder's, emptied each time."""
    from src.server.config import get_settings

    return Path(get_settings().data_dir) / "plugin-data" / "geonames"


def fetch(progress: Progress) -> int:
    """Download the five files, read them and keep them (instead of what
    was kept, in one transaction). Returns how many places: towns and
    landmarks."""
    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.repository.places import PlaceRepository

    base = get_settings().lumiverb_geonames_url.rstrip("/") + "/"
    if not base.startswith(("https://", "http://")):
        raise ValueError("LUMIVERB_GEONAMES_URL must be an http(s) address")
    here = folder()
    shutil.rmtree(here, ignore_errors=True)
    here.mkdir(parents=True)
    try:
        names = (PLACES, REGIONS, COUNTRIES, NAMES, FEATURES)
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as client:
            sizes = {}
            for name in names:
                head = client.head(base + name)
                sizes[name] = int(head.headers.get("content-length", 0)) if head.status_code == 200 else 0
            total = sum(sizes.values()) or None
            done = 0
            progress(0, total, "Downloading the place list", force=True)
            for name in names:
                done += _download(client, base + name, here / name, _MOST[name][0], progress, done, total)

        progress(done, total, "Reading the towns", force=True)
        places = list(parse_places(_member(here, PLACES)))
        regions = list(parse_regions((here / REGIONS).read_text(encoding="utf-8")))
        countries = list(parse_countries((here / COUNTRIES).read_text(encoding="utf-8")))
        if not places:
            raise ValueError(f"{PLACES} has no places in it")
        progress(done, total, "Reading which landmarks have a Wikipedia article", force=True)
        linked = wikipedia_linked(_member(here, NAMES))
        progress(done, total, "Reading the landmarks", force=True)
        landmarks = list(parse_landmarks(_member(here, FEATURES), linked))
        del linked
        if not landmarks:
            raise ValueError(f"{FEATURES} has no landmarks in it")

        progress(done, total, f"Saving {len(places):,} towns and {len(landmarks):,} landmarks", force=True)
        with get_control_session() as session:
            n = PlaceRepository(session).replace(places, regions, countries, landmarks)
            session.commit()
        return n
    finally:
        shutil.rmtree(here, ignore_errors=True)
