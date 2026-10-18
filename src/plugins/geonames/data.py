"""The place list (ADR-021's Place names): GeoNames' towns of 1,000 people
or more, their regions and countries, fetched from download.geonames.org
(or the mirror LUMIVERB_GEONAMES_URL names) and kept in the control
database, the same for every account. Only what search needs is kept:
names, points, region, country, population and time zone.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable, Iterator
from typing import Any

import httpx

PLACES, REGIONS, COUNTRIES = "cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt"
_MEMBER = "cities1000.txt"
# What it may be, at most, before it's refused: GeoNames' are 11 MB zipped, 32 MB unzipped (2026-10-18).
_MOST_DOWNLOAD = 100 * 1024 * 1024
_MOST_UNZIPPED = 300 * 1024 * 1024

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


def _download(client: httpx.Client, url: str, progress: Progress, done: int, total: int | None) -> bytes:
    buf = io.BytesIO()
    with client.stream("GET", url) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            buf.write(chunk)
            if buf.tell() > _MOST_DOWNLOAD:
                raise ValueError(f"{url} is bigger than {_MOST_DOWNLOAD // (1024 * 1024)} MB")
            progress(done + buf.tell(), total, "Downloading the place list")
    return buf.getvalue()


def fetch(progress: Progress) -> int:
    """Download the three files, read them and keep them (instead of what
    was kept, in one transaction). Returns how many places."""
    from src.server.config import get_settings
    from src.server.database import get_control_session
    from src.server.repository.places import PlaceRepository

    base = get_settings().lumiverb_geonames_url.rstrip("/") + "/"
    if not base.startswith(("https://", "http://")):
        raise ValueError("LUMIVERB_GEONAMES_URL must be an http(s) address")
    files: dict[str, bytes] = {}
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as client:
        sizes = {}
        for name in (PLACES, REGIONS, COUNTRIES):
            head = client.head(base + name)
            sizes[name] = int(head.headers.get("content-length", 0)) if head.status_code == 200 else 0
        total = sum(sizes.values()) or None
        done = 0
        progress(0, total, "Downloading the place list", force=True)
        for name in (PLACES, REGIONS, COUNTRIES):
            files[name] = _download(client, base + name, progress, done, total)
            done += len(files[name])

    progress(done, total, "Reading the place list", force=True)
    with zipfile.ZipFile(io.BytesIO(files[PLACES])) as z:
        info = z.getinfo(_MEMBER)
        if info.file_size > _MOST_UNZIPPED:
            raise ValueError(f"{_MEMBER} is bigger than {_MOST_UNZIPPED // (1024 * 1024)} MB")
        places = list(parse_places(io.TextIOWrapper(z.open(info), encoding="utf-8")))
    regions = list(parse_regions(files[REGIONS].decode("utf-8")))
    countries = list(parse_countries(files[COUNTRIES].decode("utf-8")))
    if not places:
        raise ValueError(f"{PLACES} has no places in it")

    progress(done, total, f"Saving {len(places):,} places", force=True)
    with get_control_session() as session:
        n = PlaceRepository(session).replace(places, regions, countries)
        session.commit()
    return n
