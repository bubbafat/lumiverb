"""The GeoNames plug-in (src/own_plugins/geonames/, ADR-023 phase 3 part 2)
as the plug-in runner runs it: a real process, under each python3 this
machine has (3.9 or later), fetching its list from a local mirror of the
tests' own GeoNames files (tests/plugins/geonames_lists.py), never from
geonames.org.

It says how far it's got while it fetches and builds, then answers as
today's Place names did on the same files (the golden file, written from
today's code: tests/test_geonames_parity.py); a restart uses its list, and
the downloads that finished, again; a list of an older version answers
searches while the new one is built, its work waiting meanwhile; a download
that fails, or a file that isn't GeoNames', says why and is fetched again
when it's started again; a build stopped again and again isn't tried a
fourth time; what Lumiverb won't show is cleaned or cut. It imports nothing
but Python's own and its own files, and is written for Python 3.9.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.plugins.manifest import checked_outputs, read
from src.server.plugin_client import PluginAnswerError, ask, socket_path
from src.server.plugin_install import INSTALL_MARK, program_dir
from src.server.plugin_lookups import checked_place
from src.server.plugin_runner import READY, Runner
from tests.plugins import geonames_lists as lists
from tests.test_plugin_runner import (
    _until,
    data,  # noqa: F401 (a fixture)
)

pytestmark = pytest.mark.fast
PROGRAM = Path(__file__).parents[1] / "src" / "own_plugins" / "geonames"
GOLDEN = Path(__file__).parent / "plugins" / "geonames_golden.json"
ASK = Path(__file__).parent / "plugins" / "geonames_ask.py"


def _pythons() -> list[tuple[str, str]]:
    """Each distinct python3 this machine would run it with, 3.9 or later, (version, path): the first on the
    PATH (Lumiverb's own environment's left out: no plug-in is run with it), /usr/bin/python3, and a 3.9 uv
    installed, if there's one (uv python find never downloads)."""
    ours = os.path.realpath(os.path.dirname(sys.executable))
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if os.path.realpath(p) != ours)
    found = [shutil.which("python3", path=path), "/usr/bin/python3"]
    if shutil.which("uv"):
        r = subprocess.run(["uv", "python", "find", "3.9"], capture_output=True, text=True, timeout=60, check=False)
        found.append(r.stdout.strip() if r.returncode == 0 else None)
    distinct: dict[tuple[str, str], str] = {}
    for python in found:
        if not python or not os.access(python, os.X_OK):
            continue
        r = subprocess.run([python, "-I", "-c", "import sys; print(sys.version_info >= (3, 9)); print(sys.version"
                            ".split()[0]); print(sys.base_prefix)"], capture_output=True, text=True, timeout=60,
                           check=False)
        said = r.stdout.split("\n")
        if r.returncode == 0 and said[0] == "True":
            distinct.setdefault((said[1], said[2]), python)
    return sorted(((version, python) for (version, _), python in distinct.items()), reverse=True)


PYTHONS = _pythons()
FIRST = PYTHONS[0][1]  # the one the interpreter-independent tests run under: the PATH's, where it has one


class GeoNames:
    """The plug-in installed in a data folder and run by the real plug-in runner under a python3 of this
    machine's, its mirror a test's (its server setting)."""

    def __init__(self, folder: Path, python: str, mirror: str) -> None:
        self.data, self.settings = folder, {"mirror": mirror}
        link = folder.parent / "bin" / "python3"  # the runner finds python3 here first (the data fixture's)
        link.unlink()
        link.symlink_to(python)
        program = program_dir(folder, "geonames")
        shutil.copytree(PROGRAM, program)
        (program / INSTALL_MARK).write_text(uuid.uuid4().hex)  # as an install marks it
        self.manifest = read(program)
        self.home = folder / "plugins" / "geonames" / "data"
        self.runner = self._runner()

    def _runner(self) -> Runner:
        return Runner(self.data, installed=lambda: {"geonames": SimpleNamespace(manifest=self.manifest)},
                      wanted=set, server_settings=lambda plugin_id, manifest: dict(self.settings))

    @property
    def plugin(self):
        return self.runner.plugins.get("geonames")

    def until(self, done, within: float = 60.0) -> None:
        _until(self.runner, done, within)

    def until_ready(self, within: float = 60.0) -> None:
        self.until(lambda: self.plugin is not None and self.plugin.state == READY and not self.plugin.detail, within)

    def health(self) -> dict:
        return ask(socket_path(self.data, "geonames"), "GET", "/v1/health")

    def post(self, path: str, body: dict) -> dict:
        return ask(socket_path(self.data, "geonames"), "POST", path, body=body, timeout=60)

    def places(self, q: str, limit: int = 10) -> list[dict]:
        return self.post("/v1/lookups/places", {"q": q, "limit": limit, "settings": self.settings})["places"]

    def work(self, clips: list[dict], radius_km: int = 25) -> dict:
        return self.post("/v1/work", {"settings": {"radius_km": radius_km, **self.settings}, "clips": clips})["clips"]

    def restart(self) -> None:
        self.runner.stop_all()
        self.runner = self._runner()

    def kill(self) -> None:
        """Gone at once, as the kernel ends a process for its memory: nothing of it runs after."""
        os.killpg(self.plugin.process.pid, signal.SIGKILL)
        self.plugin.process.wait()

    def stop(self) -> None:
        self.runner.stop_all()

    @property
    def list(self) -> Path:
        return self.home / "places.sqlite"


@pytest.fixture
def site(tmp_path):
    mirror = lists.Mirror(lists.write(tmp_path / "site"))
    yield mirror
    mirror.close()


def _clip(n: int, lat: float, lon: float, sure_m: float = 0.0) -> dict:
    return {"id": f"clip{n}", "location": {"lat": lat, "lon": lon, "sure_m": sure_m, "source": "file"}}


def _as_found(place: dict) -> dict:
    """A place it answered, as today's search said one."""
    return {"geonameid": int(place["id"]), **{k: place[k] for k in ("name", "region", "country", "kind", "matched",
                                                                       "lat", "lon", "population")}}


def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def as_today(g: GeoNames, python: str, today: dict) -> None:
    """Its answers are today's (answers() of today's Place names, or the golden file's): its list's own, under
    this python3; and what it says over its socket, for each search (lookups) and each clip's facts (work)."""
    r = subprocess.run([python, "-I", str(ASK), str(PROGRAM), str(g.list)], capture_output=True, text=True,
                       timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    theirs = json.loads(r.stdout)
    assert lists.differences(today, theirs) == []
    q = lists.questions()
    tie = q["nearest"].index([*lists.TWINS, 1000.0])
    assert theirs["nearest"][tie]["geonameid"] == min(lists.FULL_TIE), "a full tie: the lowest id"
    for (typed, limit), found in zip(q["search"], today["search"], strict=True):
        assert [_as_found(p) for p in g.places(typed, limit)] == found, typed
    for n, ((lat, lon, sure, radius_km), said) in enumerate(zip(q["facts"], today["facts"], strict=True)):
        outputs = g.work([_clip(n, lat, lon, sure)], radius_km)[f"clip{n}"]["outputs"]
        assert {k: (v["text"] if v else None) for k, v in outputs.items()} == said, (lat, lon, sure, radius_km)


# ---------------------------------------------------------------------------
# Its code
# ---------------------------------------------------------------------------


def test_it_imports_nothing_but_pythons_own_and_its_own_files() -> None:
    own = {p.stem for p in PROGRAM.glob("*.py")}
    for path in PROGRAM.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            for name in names:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top in own or top == "__future__", f"{path.name}: {name}"
    assert not (PROGRAM / "__init__.py").exists(), "a program of its own, not a package of Lumiverb's"


def test_it_is_written_for_python_3_9() -> None:
    for path in PROGRAM.glob("*.py"):
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))
    assert [v for v, _ in PYTHONS], "a python3 3.9 or later on this machine"


def test_its_landmarks_are_lumiverbs_until_lumiverbs_go() -> None:
    from src.plugins.geonames.landmarks import KINDS

    spec = importlib.util.spec_from_file_location("geonames_plugin_landmarks", PROGRAM / "landmarks.py")
    theirs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = theirs  # its dataclass looks itself up there
    try:
        spec.loader.exec_module(theirs)
    finally:
        del sys.modules[spec.name]
    assert {c: (k.noun, k.reach_m, k.every_one) for c, k in theirs.KINDS.items()} == {
        c: (k.noun, k.reach_m, k.every_one) for c, k in KINDS.items()}


def test_its_manifest_asks_what_place_names_asked_and_says_what_it_does() -> None:
    from src.plugins.geonames import WAS

    m = read(PROGRAM)
    assert (m.plugin_id, m.title, m.description) == (WAS.plugin_id, WAS.title, WAS.description)
    assert m.acknowledgment == WAS.acknowledgment, "the built-in's version 3, word for word: no one asked again"
    assert m.run == ("python3", "-I", "geonames.py") and not m.internet
    assert m.credit.shown() == {"text": "GeoNames", "url": "https://www.geonames.org/",
                                "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}
    assert {d.url.rsplit("/", 1)[1] for d in m.downloads} == {
        "cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt", "alternateNamesV2.zip", "allCountries.zip"}
    radius, mirror = m.settings
    assert (radius.key, radius.default, radius.minimum, radius.maximum, radius.remakes) == ("radius_km", 25, 1, 200,
                                                                                           True)
    assert (mirror.key, mirror.scope, mirror.default) == ("mirror", "server", "")
    assert [(o.key, o.searchable, o.empty) for o in m.work.outputs] == [("at", False, ""),
                                                                        ("near", True, "Not near a town")]
    assert (m.work.inputs, m.lookups) == (("location",), ("places",))


# ---------------------------------------------------------------------------
# Run as the runner runs it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("python", [p for _, p in PYTHONS], ids=[f"python{v}" for v, _ in PYTHONS])
def test_it_says_how_far_it_has_got_then_answers_as_today(data, tmp_path, python) -> None:  # noqa: F811
    """Under each python3: preparing with how far (megabytes downloaded of the whole), then ready, answering
    every question as today's Place names did on the same files, within its memory and disk."""
    mirror = lists.Mirror(lists.write(tmp_path / "site", stored_mb=4))
    mirror.hold("allCountries.zip", 2 * lists.MB)
    g = GeoNames(data, python, mirror.url)
    try:
        g.until(lambda: g.plugin.state == "preparing" and g.plugin.detail == "Downloading the place list"
                and (g.plugin.done or 0) >= 2)
        assert g.plugin.total >= 4 and g.plugin.done < g.plugin.total
        assert g.health()["state"] == "preparing"
        assert g.post("/v1/work", {"settings": {}, "clips": [_clip(0, *lists.PARIS)]})["clips"] == {
            "clip0": {"error": {"whose": "plugin", "message": "Its place list isn't fetched yet"}}}
        mirror.release()
        g.until_ready()
        as_today(g, python, _golden()["answers"])
        p = g.plugin
        assert p.restarts == 0 and (p.memory_mb or 0) < g.manifest.memory_mb
        assert not (g.home / "building").exists(), "its downloads deleted once read"
        assert sum(f.stat().st_size for f in g.home.rglob("*")) < g.manifest.disk_mb * lists.MB
    finally:
        g.stop()
        mirror.close()


def test_a_restart_uses_its_list_again_and_fetches_nothing(data, site) -> None:  # noqa: F811
    g = GeoNames(data, FIRST, site.url)
    try:
        g.until_ready()
        asked = list(site.asked)
        g.restart()
        g.until_ready()
        assert site.asked == asked, "nothing asked of the server"
        assert [p["name"] for p in g.places("paris")] == ["Paris", "Paris", "Parisot"]
    finally:
        g.stop()


def test_a_restart_mid_download_keeps_the_files_that_finished(data, tmp_path) -> None:  # noqa: F811
    """Stopped while the last file comes, it keeps the four that finished and takes nothing part-way as whole:
    started again, only that one is fetched again, and one the server now has otherwise."""
    mirror = lists.Mirror(lists.write(tmp_path / "site", stored_mb=4))
    mirror.hold("allCountries.zip", lists.MB)
    g = GeoNames(data, FIRST, mirror.url)
    try:
        g.until(lambda: (g.plugin.done or 0) >= 1 and g.plugin.detail == "Downloading the place list")
        g.stop()
        building = g.home / "building"
        assert (building / "allCountries.zip.part").exists() and not (building / "allCountries.zip").exists()
        mirror.release()
        later = time.time() + 5
        os.utime(mirror.folder / "countryInfo.txt", (later, later))  # the server's copy is newer now
        g.restart()
        g.until_ready()
        assert {name: mirror.gets(name) for name in ("cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt",
                                                     "alternateNamesV2.zip", "allCountries.zip")} == {
            "cities1000.zip": 1, "admin1CodesASCII.txt": 1, "countryInfo.txt": 2, "alternateNamesV2.zip": 1,
            "allCountries.zip": 2}
        assert [p["name"] for p in g.places("magic")] == ["Magic Kingdom, Walt Disney World"]
    finally:
        g.stop()
        mirror.close()


def _older(g: GeoNames) -> None:
    """Its list as another version of the plug-in left it (while it isn't running)."""
    with sqlite3.connect(g.list) as db:
        db.execute("UPDATE meta SET value = '0' WHERE key = 'data_version'")


def test_a_list_of_an_older_version_answers_searches_while_the_new_one_is_built(data, tmp_path) -> None:  # noqa: F811
    mirror = lists.Mirror(lists.write(tmp_path / "site", stored_mb=4))
    g = GeoNames(data, FIRST, mirror.url)
    try:
        g.until_ready()
        g.stop()
        _older(g)
        mirror.hold("allCountries.zip", lists.MB)
        g.restart()
        g.until(lambda: g.plugin.state == READY and (g.plugin.done or 0) >= 1)
        assert g.plugin.detail == "Fetching a newer place list: Downloading the place list"
        assert [p["name"] for p in g.places("paris")] == ["Paris", "Paris", "Parisot"], "the old one answers"
        assert g.work([_clip(0, *lists.PARIS)]) == {
            "clip0": {"error": {"whose": "plugin", "message": "A newer place list is being fetched"}}}, "work waits"
        mirror.release()
        g.until_ready()
        assert g.work([_clip(0, *lists.PARIS)])["clip0"]["outputs"]["near"] == {"text": "Paris, Île-de-France, France"}
    finally:
        g.stop()
        mirror.close()


def _until_building(g: GeoNames) -> None:
    """Until it's reading what it downloaded (a build under way), its health answered every time once it
    first is."""
    end, listening = time.monotonic() + 60, False
    while time.monotonic() < end:
        g.runner.turn()
        try:
            detail = g.health().get("detail") or ""
            listening = True
        except PluginAnswerError:
            assert not listening, "its health went unanswered while it fetched and built"
            detail = ""  # not listening yet
        if "Reading the landmarks" in detail:
            return
        time.sleep(0.01)
    raise AssertionError(f"never building: {g.plugin.said()}")


def test_stopped_mid_build_again_and_again_it_waits_and_keeps_the_list_in_use_whole(data, tmp_path) -> None:  # noqa: F811
    """Ended while it builds (as for its memory), the list in use still answers; stopped like that three
    times in a row on the same downloads, it builds no fourth time but says why, and a start with other
    settings builds."""
    folder = lists.write(tmp_path / "site", filler_mb=150)
    mirror = lists.Mirror(folder)
    g = GeoNames(data, FIRST, mirror.url)
    try:
        g.until_ready()
        g.stop()
        _older(g)
        for _ in range(3):
            g.restart()
            _until_building(g)
            g.kill()
            assert _names(g.list, "paris") == ["Paris", "Paris", "Parisot"], "the list in use, whole"
        g.restart()
        g.until(lambda: g.plugin.state == READY and (g.plugin.detail or "").startswith("A newer place list couldn't"))
        assert "Building the place list stopped 3 times in a row" in g.plugin.detail
        assert [p["name"] for p in g.places("paris")] == ["Paris", "Paris", "Parisot"]
        other = lists.Mirror(folder)  # other settings: another mirror of the same files
        try:
            g.settings = {"mirror": other.url}
            g.until_ready(within=120)
        finally:
            other.close()
    finally:
        g.stop()
        mirror.close()


def _names(path: Path, folded: str) -> list[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return [r[0] for r in db.execute("SELECT name FROM towns WHERE folded LIKE ?", (folded + "%",))]


@pytest.mark.parametrize("broken", ["missing", "too big", "not a zip"])
def test_a_download_that_fails_says_why_and_is_tried_again_when_it_starts_again(data, site, broken) -> None:  # noqa: F811
    good = (site.folder / "alternateNamesV2.zip").read_bytes()
    if broken == "missing":
        (site.folder / "alternateNamesV2.zip").unlink()
    elif broken == "too big":
        site.claimed["alternateNamesV2.zip"] = 2000 * lists.MB
    else:
        (site.folder / "alternateNamesV2.zip").write_bytes(b"not a zip at all")
    g = GeoNames(data, FIRST, site.url)
    try:
        g.until(lambda: g.plugin.state == "failing")
        why = {"missing": "alternateNamesV2.zip couldn't be fetched from http://127.0.0.1:",
               "too big": "alternateNamesV2.zip is bigger than 1,000 MB",
               "not a zip": "alternateNamesV2.zip isn't what GeoNames writes"}[broken]
        assert why in g.plugin.why and g.health()["state"] == "failed"
        if broken == "missing":
            assert "HTTP 404" in g.plugin.why
        assert g.plugin.process.poll() is None, "it goes on answering"
        (site.folder / "alternateNamesV2.zip").write_bytes(good)
        site.claimed.clear()
        g.restart()
        g.until_ready()
        assert [p["matched"] for p in g.places("zurigo")] == ["Zurigo"]
        if broken == "not a zip":
            assert site.gets("alternateNamesV2.zip") == 2, "what wasn't GeoNames' fetched again"
    finally:
        g.stop()


def test_with_no_mirror_it_would_reach_geonames_only_through_this_machines_proxy(data) -> None:  # noqa: F811
    """Its default is GeoNames' own server; a test's plug-ins are handed a proxy that leads nowhere
    (conftest), so it fails with the proxy's error and nothing reaches geonames.org."""
    g = GeoNames(data, FIRST, "")
    try:
        g.until(lambda: g.plugin.state == "failing")
        assert "https://download.geonames.org/export/dump/cities1000.zip" in g.plugin.why
        assert "refused" in g.plugin.why.lower() or "connect" in g.plugin.why.lower(), g.plugin.why
    finally:
        g.stop()


def test_a_lookup_is_answered_while_it_works_through_a_batch(data, site) -> None:  # noqa: F811
    g = GeoNames(data, FIRST, site.url)
    try:
        g.until_ready()
        batch = [_clip(n, lists.EIFFEL[0] + n * 1e-5, lists.EIFFEL[1]) for n in range(200)]
        done: dict = {}
        worker = threading.Thread(target=lambda: done.update(g.work(batch)))
        worker.start()
        assert [p["name"] for p in g.places("zurich")] == ["Zürich", "Zürich (Kreis 7)"]
        worker.join(120)
        assert len(done) == 200 and done["clip0"]["outputs"] == {"at": {"text": "Tour Eiffel"},
                                                                 "near": {"text": "Paris, Île-de-France, France"}}
    finally:
        g.stop()


def test_what_lumiverb_wont_show_is_cleaned_or_cut_and_what_it_says_passes_lumiverbs_checks(data, tmp_path) -> None:  # noqa: F811
    mirror = lists.Mirror(lists.write(tmp_path / "site", odd=True))
    g = GeoNames(data, FIRST, mirror.url)
    plugin = SimpleNamespace(manifest=g.manifest)
    try:
        g.until_ready()
        assert [(p["name"], p["region"]) for p in g.places("bad")] == [("BadBellbtown/b", "Cairo")]
        assert [p["matched"] for p in g.places("bell")] == ["Belltown Again"]
        for gone in ("nowhere", "offworld", "<>"):
            assert g.places(gone) == [], "no real point, or no name left"
        long = g.places("llanfair")
        assert len(long) == 1 and len(long[0]["name"]) <= 200 and long[0]["name"].endswith("the very…")
        tags = g.places("tower")
        assert [(p["name"], p["region"], p["country"]) for p in tags] == [("Tower iof/i Tags", "Gizascript", "Longland")]
        for p in (*long, *tags, *g.places("bad"), *g.places("a", 100)):
            checked_place(plugin, p)  # Lumiverb takes every place it answers
        made = g.work([_clip(0, 35.2, 35.2), _clip(1, 35.3, 35.3)], radius_km=1)
        for clip in made.values():
            checked_outputs(g.manifest.work, clip["outputs"])  # and every fact
        assert made["clip0"]["outputs"]["near"]["text"].endswith("…") and len(made["clip0"]["outputs"]["near"]["text"]) <= 200
        assert made["clip1"]["outputs"]["at"] == {"text": "Tower iof/i Tags, Gizascript, Longland"}
    finally:
        g.stop()
        mirror.close()


def test_a_clip_whose_location_it_cant_read_is_that_clips_error(data, site) -> None:  # noqa: F811
    g = GeoNames(data, FIRST, site.url)
    try:
        g.until_ready()
        made = g.work([_clip(0, *lists.PARIS), {"id": "odd", "location": {"lat": "north", "lon": 2}},
                       {"id": "nowhere"}])
        assert made["clip0"]["outputs"]["near"] == {"text": "Paris, Île-de-France, France"}
        assert made["odd"]["error"]["whose"] == "clip" and "north" in made["odd"]["error"]["message"]
        assert made["nowhere"] == {"outputs": {"at": None, "near": None}}
        assert g.places("x" * 300) == [] and g.post("/v1/lookups/places", {"id": "nope"}) == {"places": []}
        assert g.post("/v1/lookups/places", {"id": "6254976"})["places"][0]["kind"] == "monument"
    finally:
        g.stop()
