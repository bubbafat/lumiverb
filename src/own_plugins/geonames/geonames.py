"""Place names from GeoNames, as a Lumiverb plug-in (ADR-023 phase 3):
written against the plug-in protocol alone, Python's standard library and
nothing of Lumiverb's, run with the machine's python3 (3.9 or later).

It keeps its own list of the world's towns of 1,000 people or more and its
landmarks (places.py: a SQLite file in its data folder), fetched from
GeoNames (fetch.py) once it's started with none: Lumiverb starts it only
while it's on in some account, so nothing is fetched before an admin has
accepted GeoNames' license. A newer version's list is fetched the same way,
the old one answering searches meanwhile.

  GET  /v1/health          ready (with how far a newer list has got, while
                           one is fetched); preparing, with how far; failed,
                           with why
  POST /v1/work            each clip's location -> the landmark it's at
                           ("landmark") and the nearest town within its
                           radius ("town"), as facts; while it has no list of this
                           version, every clip's error is its own (whose:
                           plugin): they wait, uncharged
  POST /v1/lookups/places  {"q", "limit"} or {"id"}: places by name, or one
                           by its GeoNames id

Its environment (Lumiverb's): LUMIVERB_PLUGIN_SOCKET, LUMIVERB_PLUGIN_DATA,
LUMIVERB_PLUGIN_SETTINGS (its server settings: the mirror to fetch from),
and this machine's proxy, if any.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # run with -I, which leaves its own folder off its path (Python 3.11 and later)

import fetch  # noqa: E402 — its own files, found once its folder is on its path
import places  # noqa: E402

RETRY_SEC = 3600.0  # a list that couldn't be fetched or built is tried again this long after
STRIKES = 3  # builds cut off in a row (killed for its memory, say) before it waits RETRY_SEC to build again
LIMIT_MAX = 100


def _mb(n: int) -> int:
    return n // fetch.MB


class Keeper:
    """Its place list: the one in use (read by every request), and a new one built on a thread of its own
    while there's none of this version. Every request's view of it is health()'s."""

    def __init__(self, data: str, mirror: str) -> None:
        self.data, self.mirror = Path(data), mirror
        self.list = str(self.data / "places.sqlite")
        self.building = self.data / "building"
        self._lock = threading.Lock()
        self._said: dict[str, Any] = {}  # how far the build has got: detail, done, total
        self.error: str | None = None
        self.version = places.version_of(self.list)
        if self.current() and self.building.exists():
            shutil.rmtree(self.building, ignore_errors=True)  # what a build left once its list was in place

    def current(self) -> bool:
        return self.version == places.DATA_VERSION

    def health(self) -> dict[str, Any]:
        with self._lock:
            said, error, version = dict(self._said), self.error, self.version
        if version == places.DATA_VERSION:
            return {"state": "ready"}
        if version is not None:  # an older list answers meanwhile
            if error:
                return {"state": "ready", "detail": f"A newer place list couldn't be fetched: {error}"}
            return {"state": "ready", **said, "detail": f"Fetching a newer place list: {said.get('detail', '')}"
                    .rstrip(": ")}
        if error:
            return {"state": "failed", "error": error}
        return {"state": "preparing", "detail": "Getting ready", **said}

    def _say(self, detail: str, done: int | None = None, total: int | None = None) -> None:
        with self._lock:
            self._said = {"detail": detail, "done": done, "total": total}

    # --- fetching and building ------------------------------------------------------

    def keep(self) -> None:
        """Until there's a list of this version: fetched and built, tried again RETRY_SEC after a failure."""
        let_through = False
        while not self.current():
            try:
                self._build(let_through)
            except fetch.FetchError as exc:
                self._failed(str(exc))
            except Exception as exc:  # noqa: BLE001 — said, and tried again later: it goes on answering
                self._failed(f"{type(exc).__name__}: {exc}")
            else:
                continue
            let_through = True  # an hour on, a build stopped again and again is tried once more
            time.sleep(RETRY_SEC)

    def _failed(self, why: str) -> None:
        with self._lock:
            self.error = why[:400]
        print(f"The place list couldn't be made: {why}. It's tried again in an hour, or when it's started again",
              flush=True)

    def _build(self, let_through: bool) -> None:
        with self._lock:
            self.error = None  # tried again: how far it gets is said meanwhile
        base = fetch.base_of(self.mirror)
        self.building.mkdir(parents=True, exist_ok=True)
        self._say("Looking up GeoNames' lists")
        heads = {name: fetch.head(base, name) for name in fetch.FILES}
        total = sum(h["length"] or 0 for h in heads.values())
        got = [0]

        def seen(n: int) -> None:
            got[0] += n
            self._say("Downloading the place list", _mb(got[0]), _mb(total) or None)

        for name in fetch.FILES:
            if fetch.kept(self.building, name, heads[name]):
                print(f"Using {name}, downloaded before", flush=True)
                seen(heads[name]["length"])
            else:
                print(f"Downloading {name} from {base}", flush=True)
                fetch.download(base, self.building, name, seen)

        key = json.dumps({"base": base, "heads": heads}, sort_keys=True)  # what it's built from: a strike's
        strikes = self._strikes(key)
        if strikes >= STRIKES and not let_through:
            raise fetch.FetchError(f"Building the place list stopped {strikes} times in a row (killed for using too "
                                   "much memory, perhaps)")
        self._write_strikes(key, strikes + 1)
        started = time.monotonic()
        try:
            counts = self._read(base)
        except fetch.BadFileError as exc:
            fetch.forget(self.building, exc.name)  # fetched again
            self._write_strikes(key, 0)
            raise
        except Exception:
            self._write_strikes(key, 0)  # it stopped by itself, with why: not a strike
            raise
        os.replace(self.building / "places.sqlite", self.list)
        fd = os.open(self.data, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        shutil.rmtree(self.building, ignore_errors=True)
        with self._lock:
            self.version, self.error, self._said = places.DATA_VERSION, None, {}
        print(f"The place list is ready, built in {time.monotonic() - started:.0f} s: {counts['towns']:,} towns, "
              f"{counts['landmarks']:,} landmarks, {counts['names']:,} names in other languages", flush=True)

    def _read(self, base: str) -> dict[str, int]:
        """GeoNames' files, read into a new list beside the one in use: its counts."""
        folder = self.building
        build = places.Builder(str(folder / "places.sqlite"))
        try:
            build.countries(fetch.parse_countries(fetch.text(folder, fetch.COUNTRIES)))
            build.regions(fetch.parse_regions(fetch.text(folder, fetch.REGIONS)))
            build.towns(fetch.parse_places(self._lines(fetch.PLACES, "Reading the towns")))
            if not build.counts()["towns"]:
                raise fetch.BadFileError(fetch.PLACES, "no towns in it")
            linked = fetch.wikipedia_linked(self._lines(fetch.NAMES, "Reading which landmarks have a Wikipedia "
                                                                     "article"))
            build.landmarks(fetch.parse_landmarks(self._lines(fetch.FEATURES, "Reading the landmarks"), linked))
            del linked
            counts = build.counts()
            if not counts["landmarks"]:
                raise fetch.BadFileError(fetch.FEATURES, "no landmarks in it")
            build.names(fetch.parse_names(self._lines(
                fetch.NAMES, f"Saving {counts['towns']:,} towns and {counts['landmarks']:,} landmarks, with their "
                             "names in other languages"), build.own, places.fold))
            build.own.clear()
            self._say("Indexing the place list")
            return build.finish({"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": base})
        except BaseException:
            build.abandon()
            raise

    def _lines(self, name: str, detail: str):
        total, got = fetch.size_of(self.building, name), [0]
        self._say(detail, 0, _mb(total))

        def seen(n: int) -> None:
            got[0] += n
            self._say(detail, _mb(got[0]), _mb(total))

        return fetch.lines(self.building, name, seen)

    def _strikes(self, key: str) -> int:
        try:
            said = json.loads((self.building / "strikes.json").read_text())
        except (OSError, ValueError):
            return 0
        return int(said.get("count", 0)) if said.get("key") == key else 0

    def _write_strikes(self, key: str, count: int) -> None:
        path = self.building / "strikes.json"
        path.write_text(json.dumps({"key": key, "count": count}))


# ---------------------------------------------------------------------------
# What it answers
# ---------------------------------------------------------------------------


def facts(found: places.Places, where: dict[str, Any] | None, within_m: float) -> dict[str, Any]:
    """A clip's facts from where it is: the landmark it's at ("landmark": its name when a town is near, else
    its label, which says where) and the nearest town ("town": its label), as the Lightbox has said them."""
    if not where:
        return {"landmark": None, "town": None}
    lat, lon, sure = float(where["lat"]), float(where["lon"]), float(where.get("sure_m") or 0)
    town = found.nearest(lat, lon, within_m)
    mark = found.landmark_at(lat, lon, sure)
    return {"landmark": {"text": places.cut(mark["name"] if town else places.label(mark), places.FACT_MAX)}
            if mark else None,
            "town": {"text": places.cut(places.label(town), places.FACT_MAX)} if town else None}


def work(keeper: Keeper, sent: dict[str, Any]) -> dict[str, Any]:
    settings, clips = sent.get("settings") or {}, [c for c in sent.get("clips") or [] if isinstance(c, dict)]
    if not keeper.current():
        why = ("A newer place list is being fetched" if keeper.version is not None
               else "Its place list isn't fetched yet")
        return {"clips": {c["id"]: {"error": {"whose": "plugin", "message": why}} for c in clips if "id" in c}}
    within_m = float(settings.get("radius_km") or 25) * 1000
    out: dict[str, Any] = {}
    with places.Places(keeper.list) as found:
        for clip in clips:
            if "id" not in clip:
                continue
            try:
                out[clip["id"]] = {"outputs": facts(found, clip.get("location"), within_m)}
            except Exception as exc:  # noqa: BLE001 — that clip's, said with it
                out[clip["id"]] = {"error": {"whose": "clip", "message": f"{type(exc).__name__}: {exc}"[:400]}}
    return {"clips": out}


def shown(row: dict[str, Any]) -> dict[str, Any]:
    """A place as the protocol says it, each part no longer than Lumiverb takes."""
    return {"id": str(row["geonameid"]), "name": places.cut(row["name"], places.NAME_MAX),
            "region": places.cut(row.get("region"), places.WHERE_MAX),
            "country": places.cut(row.get("country"), places.WHERE_MAX),
            "kind": places.cut(row.get("kind"), places.KIND_MAX), "matched": places.cut(row.get("matched"),
                                                                                       places.NAME_MAX),
            "lat": row["lat"], "lon": row["lon"], "population": row["population"]}


def lookup(keeper: Keeper, sent: dict[str, Any]) -> dict[str, Any]:
    with places.Places(keeper.list) as found:
        if "id" in sent:
            own = str(sent["id"])
            row = found.get(int(own)) if own.isdigit() else None
            return {"places": [shown(row)] if row else []}
        limit = min(max(int(sent.get("limit") or 10), 1), LIMIT_MAX)
        return {"places": [shown(r) for r in found.search(str(sent.get("q") or ""), limit)]}


class Handler(BaseHTTPRequestHandler):
    keeper: Keeper

    def address_string(self) -> str:  # a Unix socket's peer has no host
        return "lumiverb"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — http.server's name
        pass

    def do_GET(self) -> None:  # noqa: N802 — http.server's name
        if self.path == "/v1/health":
            self._answer(200, self.keeper.health())
        else:
            self._answer(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — http.server's name
        try:
            sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(sent, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            self._answer(400, {"error": f"not JSON it understands ({exc})"})
            return
        if self.path == "/v1/work":
            self._answer(200, work(self.keeper, sent))
        elif self.path == "/v1/lookups/places":
            if self.keeper.version is None:
                self._answer(503, {"error": "Its place list isn't fetched yet"})
                return
            try:
                self._answer(200, lookup(self.keeper, sent))
            except (TypeError, ValueError) as exc:
                self._answer(400, {"error": f"not a lookup it understands ({exc})"})
        else:
            self._answer(404, {"error": "not found"})

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("lumiverb", 0)


def server_settings() -> dict[str, Any]:
    """Its server settings as Lumiverb handed them at its start (none: its defaults)."""
    path = os.environ.get("LUMIVERB_PLUGIN_SETTINGS")
    try:
        with open(path or "") as f:
            said = json.load(f)
    except (OSError, ValueError):
        return {}
    return said if isinstance(said, dict) else {}


def main() -> None:
    keeper = Keeper(os.environ["LUMIVERB_PLUGIN_DATA"], str(server_settings().get("mirror") or ""))
    Handler.keeper = keeper
    path = os.environ["LUMIVERB_PLUGIN_SOCKET"]
    if os.path.exists(path):
        os.unlink(path)
    with Server(path, Handler) as server:
        if keeper.current():
            print("Place names are ready", flush=True)
        else:
            print("Place names: fetching its place list" + (" (a newer one: the old one answers meanwhile)"
                                                           if keeper.version is not None else ""), flush=True)
            threading.Thread(target=keeper.keep, name="build", daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
