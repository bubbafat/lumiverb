"""GeoNames' files: fetched from its server (or the mirror its server setting
names) into the folder a list is built in, and read as GeoNames writes them.

Five files: the towns of 1,000 people or more, their regions and countries,
every feature (for its landmarks: landmarks.py) and the alternate names
(their names in other languages, and which have a Wikipedia article). Each
is written as <name>.part and renamed once it's all there, with what the
server said of it beside it (<name>.head: its size and date): a download
that finished is used again after a restart while the server still says the
same of it, and a .part never is. The zips are read as they're unzipped,
never whole into memory.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from landmarks import KINDS

GEONAMES = "https://download.geonames.org/export/dump/"
PLACES, REGIONS, COUNTRIES = "cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt"
FEATURES, NAMES = "allCountries.zip", "alternateNamesV2.zip"
FILES = (PLACES, REGIONS, COUNTRIES, NAMES, FEATURES)  # in the order they're fetched
MEMBERS = {PLACES: "cities1000.txt", FEATURES: "allCountries.txt", NAMES: "alternateNamesV2.txt"}
MB = 1024 * 1024
# What each may be, at most, downloaded and unzipped, before it's refused. GeoNames' (2026-10-18):
# 11 MB and 32 MB; 403 MB and about 1.6 GB; 195 MB and about 0.7 GB; the two lists well under 1 MB.
MOST = {PLACES: (100 * MB, 300 * MB), FEATURES: (1500 * MB, 6000 * MB), NAMES: (1000 * MB, 4000 * MB),
        REGIONS: (20 * MB, None), COUNTRIES: (20 * MB, None)}
TIMEOUT_SEC = 60.0  # a server saying nothing for this long is given up on
CHUNK = MB
AGENT = "Lumiverb's GeoNames plug-in"

Seen = Callable[[int], None]  # bytes more, for how far it's got
# An alternate name's language: ISO 639's two or three letters (with a variant: "zh-Hant"), or none.
# GeoNames' other codes (link, post, iata, icao, abbr, wkdt, ...) are no one's name for a place.
_LANGUAGE = re.compile(r"^([a-z]{2,3}(-[A-Za-z0-9]{1,8})?)?$")


class FetchError(Exception):
    """A file that couldn't be fetched, and why, as its health says it."""


class BadFileError(FetchError):
    """A downloaded file that isn't what GeoNames writes: it's fetched again."""

    def __init__(self, name: str, why: str) -> None:
        super().__init__(f"{name} isn't what GeoNames writes ({why})")
        self.name = name


def base_of(mirror: str) -> str:
    """Where the files are: the mirror, else GeoNames' own server."""
    base = (mirror or GEONAMES).strip().rstrip("/") + "/"
    if not base.startswith(("https://", "http://")):
        raise FetchError(f"Its mirror isn't an http or https address: {mirror[:100]}")
    return base


def _open(url: str, name: str, method: str = "GET"):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": AGENT})
    try:
        return urllib.request.urlopen(request, timeout=TIMEOUT_SEC)  # noqa: S310 — its server's, http(s) only
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{name} couldn't be fetched from {url}: the server answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"{name} couldn't be fetched from {url}: {getattr(exc, 'reason', exc)}") from exc


def _length(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def head(base: str, name: str) -> dict[str, Any]:
    """What the server says of a file now: {length, modified} (either None when it doesn't say)."""
    with _open(base + name, name, "HEAD") as r:
        said = {"length": _length(r.headers.get("Content-Length")), "modified": r.headers.get("Last-Modified")}
    if said["length"] is not None and said["length"] > MOST[name][0]:
        raise FetchError(f"{name} is bigger than {MOST[name][0] // MB:,} MB")
    return said


def kept(folder: Path, name: str, said: dict[str, Any]) -> bool:
    """Whether the download of this file there is the one the server has now (all of it)."""
    try:
        was = json.loads((folder / f"{name}.head").read_text())
        size = (folder / name).stat().st_size
    except (OSError, ValueError):
        return False
    return said["length"] is not None and said["modified"] is not None and was == said and size == said["length"]


def forget(folder: Path, name: str) -> None:
    for path in (folder / name, folder / f"{name}.head", folder / f"{name}.part"):
        if path.exists():
            path.unlink()


def download(base: str, folder: Path, name: str, seen: Seen) -> None:
    """The file into the folder, refused past its most, kept only once it's all there."""
    forget(folder, name)
    part, most = folder / f"{name}.part", MOST[name][0]
    with _open(base + name, name) as r, part.open("wb") as out:
        length = _length(r.headers.get("Content-Length"))
        said = {"length": length, "modified": r.headers.get("Last-Modified")}
        size = 0
        while True:
            try:
                chunk = r.read(CHUNK)
            except OSError as exc:
                raise FetchError(f"{name} stopped coming from {base}: {exc}") from exc
            if not chunk:
                break
            size += len(chunk)
            if size > most:
                raise FetchError(f"{name} is bigger than {most // MB:,} MB")
            out.write(chunk)
            seen(len(chunk))
        if length is not None and size != length:
            raise FetchError(f"{name} came cut short: {size:,} of its {length:,} bytes")
        out.flush()
        os.fsync(out.fileno())
    os.replace(part, folder / name)
    (folder / f"{name}.head").write_text(json.dumps(said))


class _Counted(io.RawIOBase):
    """A stream that says how much of it was read."""

    def __init__(self, raw: Any, seen: Seen) -> None:
        self._raw, self._seen = raw, seen

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        n = self._raw.readinto(buffer)
        self._seen(n or 0)
        return n


def size_of(folder: Path, name: str) -> int:
    """How big a downloaded file is to read: a zip's file unzipped."""
    try:
        with zipfile.ZipFile(folder / name) as z:
            return z.getinfo(MEMBERS[name]).file_size
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise BadFileError(name, str(exc) or type(exc).__name__) from exc


def lines(folder: Path, name: str, seen: Seen) -> Iterator[str]:
    """A downloaded zip's file, a line at a time as it's unzipped (refused past its most); BadFileError when it isn't
    one GeoNames writes."""
    try:
        with zipfile.ZipFile(folder / name) as z:
            info = z.getinfo(MEMBERS[name])
            most = MOST[name][1]
            if most is not None and info.file_size > most:
                raise BadFileError(name, f"{MEMBERS[name]} is bigger than {most // MB:,} MB")
            with z.open(info) as raw:
                yield from io.TextIOWrapper(io.BufferedReader(_Counted(raw, seen)), encoding="utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, zlib.error, EOFError) as exc:
        raise BadFileError(name, str(exc) or type(exc).__name__) from exc


def text(folder: Path, name: str) -> str:
    """A downloaded list, whole (they're small)."""
    try:
        return (folder / name).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BadFileError(name, str(exc)) from exc


# What follows reads GeoNames' files as Lumiverb's built-in Place names read them (src/plugins/geonames/data.py
# until it goes: the parity tests hold the two to the same answers).


def parse_places(text: Iterator[str]) -> Iterator[dict[str, Any]]:
    """GeoNames' cities file: tab-separated, 19 columns a place. A line that
    isn't one is skipped."""
    for line in text:
        p = line.rstrip("\n").split("\t")
        if len(p) < 19 or not p[0].isdigit():
            continue
        try:
            yield {"geonameid": int(p[0]), "name": p[1], "ascii_name": p[2] or p[1], "lat": float(p[4]),
                   "lon": float(p[5]), "feature_code": p[7], "country_code": p[8], "admin1_code": p[10],
                   "population": int(p[14] or 0)}
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


def wikipedia_linked(text: Iterator[str]) -> set[int]:
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


def parse_landmarks(text: Iterator[str], linked: set[int]) -> Iterator[dict[str, Any]]:
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


def parse_names(text: Iterator[str], own: dict[int, tuple[str, ...]],
                fold: Callable[[str], str]) -> Iterator[dict[str, Any]]:
    """The other names of the places kept (own: each one's own names, folded):
    alternateNamesV2.txt's rows (id, geonameid, language, name, preferred,
    short, colloquial, historic, from, to) in a language or none, neither
    colloquial nor historic, that don't fold to the place's own name. The same
    name twice for a place is the caller's to keep once."""
    for line in text:
        p = line.rstrip("\n").split("\t")
        if len(p) < 8 or not p[1].isdigit():
            continue
        geonameid = int(p[1])
        names = own.get(geonameid)
        name = p[3].strip()
        if names is None or not name or p[6] == "1" or p[7] == "1" or not _LANGUAGE.match(p[2]):
            continue
        folded = fold(name)
        if folded and folded not in names:
            yield {"geonameid": geonameid, "name": name, "folded": folded, "language": p[2]}
