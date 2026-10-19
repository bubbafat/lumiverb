"""The tester's acceptance tests for ADR-023 phase 3 (GeoNames moved out),
written apart from the builder's: through the protocol and the API, a real
install's database, the real plug-in runner, and real plug-in processes run
by this machine's own Python interpreters (PATH's python3, /usr/bin/python3,
uv's 3.9 where it's installed: never the test's own), not copies of
Lumiverb's code.

Part 1, what a real plug-in needs (the plan's section 4), with a probe
plug-in (tests/plugins/probe/) that says what it was started with: it runs
only while it's on in some account, whichever account that is; it's handed
its server settings at start, in a file only Lumiverb's user can read, and
started again when they change (an account's settings don't); its
environment holds the proxies and nothing else of Lumiverb's; it may be
ready with work in the background; an output's empty line and a credit's
license show, and the empty line isn't searched. An account whose database
can't be read, or hangs (never answers, or drops every packet: part 1b),
holds up no other.

Part 2, the GeoNames program (src/own_plugins/geonames/), on the tester's
own made-up GeoNames files (tests/tester_geonames_lists.py, apart from the
builder's), run by the real runner and asked through Lumiverb's own client
(its facts, searches and places by id were held to the built-in Place
names' on the same files until part 4 removed that code): killed, it's
started again with its list and
fetches nothing; a file missing from the mirror fails naming it; a download
cut off is fetched again, the finished ones aren't.

Part 3, the switch-over: what the built-in's producer could be (paused and
resumed on the machine, `lumiverb-server pause|resume`), the plug-in's work
can be.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, make_url, text

from src.producers import INSTALLED, use_added
from src.producers.plugin_work import PluginWork
from src.server.config import get_settings
from src.server.database import get_tenant_session, tenant_database_url
from src.server.lifecycle import cli
from src.server.plugin_producers import refresh
from src.server.plugin_runner import HANDED_ON
from tests.clips import make_clip
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.real_account import make_database
from tests.test_first_run_setup import _env_file, control  # noqa: F401 (control: a fixture)
from tests.test_plugin_install import _listed
from tests.test_plugin_runner import _until
from tests.test_plugin_runner import data as short_data  # noqa: F401 (a fixture)
from tests.test_server_admins import (
    INSTALL_KEY,
    install,  # noqa: F401 (a fixture)
)

pytestmark = pytest.mark.slow

PROBE = Path(__file__).parent / "plugins" / "probe"
KIND = "plugin_probe"
LICENSE = {"text": "CC0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"}
CREDIT = {"text": "Probe's places", "url": "https://example.org/probe", "license": LICENSE}


def _interpreters() -> list[tuple[str, str]]:
    """This machine's own Pythons, each once: (path, version). PATH's python3 (not this test's virtualenv's),
    /usr/bin/python3, and uv's 3.9 where one is installed."""
    venv = Path(sys.prefix).resolve()
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if p and not Path(p).resolve().is_relative_to(venv))
    found = [shutil.which("python3", path=path), "/usr/bin/python3"]
    uv = shutil.which("uv", path=path)
    if uv:
        said = subprocess.run([uv, "python", "find", "--no-project", "3.9"], capture_output=True, text=True,
                              check=False)
        found.append(said.stdout.strip() if said.returncode == 0 else None)
    out: dict[str, tuple[str, str]] = {}
    for candidate in found:
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        said = subprocess.run([candidate, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                              capture_output=True, text=True, check=False)
        if said.returncode == 0:
            out.setdefault(said.stdout.strip(), (candidate, said.stdout.strip()))
    return sorted(out.values(), key=lambda x: x[1])


INTERPRETERS = _interpreters()

# For what doesn't depend on the plug-in's own Python (Lumiverb's side of it): one of them, so the file stays quick.
ONE_PYTHON = pytest.mark.parametrize("machine_python", INTERPRETERS[-1:], ids=[f"python{INTERPRETERS[-1][1]}"],
                                     indirect=True)


@pytest.fixture(params=INTERPRETERS, ids=[f"python{v}" for _, v in INTERPRETERS])
def machine_python(request, short_data):  # noqa: F811
    """The runner's python3 is this machine's own (the short data folder's bin/python3, first on PATH)."""
    path, version = request.param
    link = short_data.parent / "bin" / "python3"
    link.unlink()
    link.symlink_to(path)
    return version


@pytest.fixture
def probe(install, control, short_data, machine_python, tmp_path, monkeypatch):  # noqa: F811
    """The probe installed off by default, the runner not yet turned; this account (A) with a library of two
    located photos, and a second account (B) with its own admin and database."""
    from src.server.plugin_runner import Runner

    client, robert, tenant_id = install
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    assert cli.main(["plugins", "install", str(PROBE), "--off", "--yes", "--env", str(env)]) == 0
    refresh()
    r = client.post("/v1/admin/tenants", json={"name": "Other", "plan": "free"},
                    headers={"Authorization": f"Bearer {INSTALL_KEY}"})
    assert r.status_code == 200, r.text
    other_id, other = r.json()["tenant_id"], {"Authorization": f"Bearer {r.json()['api_key']}"}
    make_database(control, other_id)
    runner = Runner(short_data)
    try:
        r = client.post("/v1/libraries", json={"name": "Probe", "root_path": "/Volumes/probe"}, headers=robert)
        assert r.status_code == 200, r.text
        library_id = r.json()["library_id"]
        with get_tenant_session(tenant_id) as session:
            clips = [make_clip(session, library_id, name) for name in ("a.jpg", "b.jpg")]
            session.execute(text("UPDATE assets SET gps_lat = 10.6, gps_lon = -61.5 WHERE asset_id = ANY(:ids)"),
                            {"ids": clips})
            session.commit()
        yield SimpleNamespace(client=client, a=robert, b=other, tenant_id=tenant_id, other_id=other_id,
                              library_id=library_id, clips=clips, runner=runner, data=short_data, env=env,
                              python=machine_python, control=control)
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)


def _running(f) -> dict:
    return _listed(f.client, f.a)["probe"]["running"]


def _started(f) -> dict | None:
    """What the probe last said it was started with, None before it ever started."""
    path = f.data / "plugins" / "probe" / "data" / "started.json"
    return json.loads(path.read_text()) if path.exists() else None


def _switch(f, who: dict, on: bool) -> None:
    r = f.client.put("/v1/plugins/probe", json={"enabled": on}, headers=who)
    assert r.status_code == 200, r.text


def _turns(f, n: int = 10) -> None:
    for _ in range(n):
        f.runner.turn()


# ---------------------------------------------------------------------------
# It runs only while it's on somewhere
# ---------------------------------------------------------------------------


def test_part1_it_runs_while_any_account_has_it_on_under_this_machines_python(probe):
    f = probe
    _until(f.runner, lambda: _running(f)["state"] == "off")
    _turns(f)
    assert _started(f) is None, "off in every account: never started"
    assert f.runner.plugins["probe"].process is None

    _switch(f, f.b, True)  # the other account, not the one asking
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    started = _started(f)
    assert started["python"] == f.python, "run by this machine's python3, not the test's"
    assert _listed(f.client, f.a)["probe"]["state"] == "off", "still off in this account"

    _switch(f, f.b, False)
    _until(f.runner, lambda: _running(f)["state"] == "off")
    assert f.runner.plugins["probe"].process is None

    # On by default: running though nobody chose; this account turning it off leaves the other following it.
    assert cli.main(["plugins", "default", "probe", "on", "--yes", "--env", str(f.env)]) == 0
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    _switch(f, f.a, False)
    _turns(f)
    assert _running(f)["state"] == "ready", "B follows the default: still on somewhere"
    _switch(f, f.b, False)  # B never chose: its choosing off leaves it on nowhere
    _until(f.runner, lambda: _running(f)["state"] == "off")


@ONE_PYTHON
def test_part1_one_unreadable_account_doesnt_keep_another_from_turning_it_on(probe):
    """An active account whose database can't be read (dropped, its server away) must not freeze what the
    runner runs for every other account. A second plug-in on nowhere (hello) makes the runner read every
    account, whatever order they're listed in."""
    f = probe
    hello = Path(__file__).parent / "plugins" / "hello"
    assert cli.main(["plugins", "install", str(hello), "--off", "--yes", "--env", str(f.env)]) == 0
    r = f.client.post("/v1/admin/tenants", json={"name": "Broken", "plan": "free"},
                      headers={"Authorization": f"Bearer {INSTALL_KEY}"})
    assert r.status_code == 200, r.text
    broken = r.json()["tenant_id"]
    make_database(f.control, broken)
    _until(f.runner, lambda: _running(f)["state"] == "off")
    engine = create_engine(make_url(f.control).set(database="postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{broken}" WITH (FORCE)'))
    engine.dispose()
    _switch(f, f.a, True)
    _until(f.runner, lambda: _running(f)["state"] == "ready", within=30)


class _Silent:
    """A TCP port that takes each connection and never says a word: a database server that's hung, or a network
    dropping its packets once connected."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        threading.Thread(target=self._take, daemon=True).start()

    def _take(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)

    def close(self) -> None:
        self.sock.close()
        for conn in self.held:
            conn.close()


@ONE_PYTHON
@pytest.mark.parametrize("hang", ["never answers", "drops packets"])
def test_part1b_an_account_database_that_hangs_doesnt_stall_the_runner(probe, hang, monkeypatch):
    """An active account whose database server takes the connection and never answers, or whose address drops
    every packet (192.0.2.1, TEST-NET-1: nothing answers there): the runner still starts a plug-in another
    account turns on, and keeps saying how its plug-ins are (its state file fresh, read by the API), sync after
    sync, while that account hangs. The runner's connections give up as its main() makes them
    (set_connect_timeout(CONNECT_WITHIN_SEC)), for the engines made from now on: the hung account's."""
    from src.server import database, plugin_runner

    monkeypatch.setattr(database, "CONNECT_TIMEOUT_SEC", database.CONNECT_TIMEOUT_SEC)  # put back afterwards
    database.set_connect_timeout(plugin_runner.CONNECT_WITHIN_SEC)
    f = probe
    hello = Path(__file__).parent / "plugins" / "hello"
    assert cli.main(["plugins", "install", str(hello), "--off", "--yes", "--env", str(f.env)]) == 0
    r = f.client.post("/v1/admin/tenants", json={"name": "Hung", "plan": "free"},
                      headers={"Authorization": f"Bearer {INSTALL_KEY}"})
    assert r.status_code == 200, r.text
    hung = r.json()["tenant_id"]
    silent = _Silent()
    where = f"127.0.0.1:{silent.port}" if hang == "never answers" else "192.0.2.1:5432"
    engine = create_engine(f.control)
    with engine.begin() as conn:
        conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                     {"u": f"postgresql+psycopg2://lumiverb:x@{where}/{hung}", "t": hung})
    engine.dispose()
    _switch(f, f.a, True)
    turns: list[float] = []
    done = threading.Event()

    def turning() -> None:
        while not done.is_set():
            started = time.monotonic()
            f.runner.turn()
            turns.append(time.monotonic() - started)
            time.sleep(0.05)

    thread = threading.Thread(target=turning, daemon=True)
    thread.start()
    try:
        end = time.monotonic() + 120
        while time.monotonic() < end and _running(f)["state"] != "ready":
            time.sleep(0.5)
        assert _running(f)["state"] == "ready", (
            f"never started while the account hung: {len(turns)} turns finished, the last {turns[-3:]}")
        if hang == "never answers":
            assert silent.held, "the runner did try the hung account"
        # Sync after sync while it hangs (each look waiting out the hung account), the plug-in is still said to
        # be ready, and what the runner says (runner.json, which the API calls stale after 60 s) is written again
        # and again, never left for long.
        state, seen, written = f.data / "plugins" / "runner.json", [], []
        until = time.monotonic() + 15
        while time.monotonic() < until:
            seen.append(_running(f)["state"])
            at = json.loads(state.read_text())["at"]
            if not written or written[-1][1] != at:
                written.append((time.monotonic(), at))
            time.sleep(0.5)
        gaps = [b[0] - a[0] for a, b in zip(written, written[1:], strict=False)]
        assert set(seen) == {"ready"}, f"{seen.count('ready')} of {len(seen)} ready; turns took {sorted(turns)[-3:]}"
        assert len(written) >= 5 and max(gaps) < 10, f"the state file written {len(written)} times, gaps {gaps}"
    finally:
        done.set()
        silent.close()
        thread.join(timeout=60)


# ---------------------------------------------------------------------------
# Its server settings at start, in a file of its own
# ---------------------------------------------------------------------------


@ONE_PYTHON
def test_part1_its_server_settings_are_a_private_file_at_start_and_a_change_restarts_it(probe):
    f = probe
    _switch(f, f.a, True)
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    started = _started(f)
    said = started["server_settings"]
    data_folder = f.data / "plugins" / "probe" / "data"
    assert Path(said["path"]).resolve().parent == (f.data / "plugins" / "probe").resolve()
    assert not Path(said["path"]).resolve().is_relative_to(data_folder.resolve()), "outside its data folder"
    assert said["mode"] == 0o600
    assert said["values"] == {"health": "ready", "mirror": "", "key": ""}, "the server's only, each its default"

    pid = started["pid"]
    r = f.client.put("/v1/plugins/probe/settings", json={"settings": {"word": "Changed"}}, headers=f.a)
    assert r.status_code == 200, r.text
    _turns(f, 20)
    assert _started(f)["pid"] == pid, "an account's setting comes with each request: no restart"

    r = f.client.put("/v1/plugins/probe/server-settings",
                     json={"settings": {"mirror": "http://127.0.0.1:1/lists/", "key": "s3cret"}}, headers=f.a)
    assert r.status_code == 200, r.text
    _until(f.runner, lambda: (_started(f) or {}).get("pid") != pid and _running(f)["state"] == "ready")
    again = _started(f)["server_settings"]
    assert again["values"] == {"health": "ready", "mirror": "http://127.0.0.1:1/lists/", "key": "s3cret"}
    assert again["mode"] == 0o600
    assert "s3cret" not in json.dumps(_listed(f.client, f.a)["probe"]), "a secret is never shown again"


# ---------------------------------------------------------------------------
# Its environment: the proxies, nothing else of Lumiverb's
# ---------------------------------------------------------------------------


def test_part1_its_environment_has_the_proxies_and_nothing_else_of_lumiverbs(probe, monkeypatch):
    f = probe
    monkeypatch.setenv("LUMIVERB_TESTER_SENTINEL", "must-not-reach-a-plug-in")
    _switch(f, f.a, True)
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    started = _started(f)
    env = started["environment"]
    own = {"HOME", "TMPDIR", "PYTHONUNBUFFERED", "LUMIVERB_PLUGIN_ID", "LUMIVERB_PLUGIN_PROTOCOL",
           "LUMIVERB_PLUGIN_SOCKET", "LUMIVERB_PLUGIN_DATA", "LUMIVERB_PLUGIN_SETTINGS"}
    # What the interpreter and the system add by themselves, started with nothing (a Mac's
    # __CF_USER_TEXT_ENCODING; Python's own LC_CTYPE when the locale is C, PEP 538): not handed on by Lumiverb.
    itself = json.loads(subprocess.run(
        [started["executable"], "-I", "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        env={}, capture_output=True, text=True, check=True).stdout)
    leaked = {k for k in env if k in os.environ and k not in own and k not in HANDED_ON and k not in itself}
    assert leaked == set(), f"of this process's environment, only the handed-on names: {sorted(leaked)}"
    for secret in ("CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE", "JWT_SECRET", "ADMIN_KEY",
                   "DATA_DIR", "LUMIVERB_TESTER_SENTINEL"):
        assert secret not in env
    # The closed proxies the suite hands every plug-in (conftest), with the test's own list reachable.
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert "127.0.0.1" in env["NO_PROXY"].split(",")
    assert env["HOME"] == str(f.data / "plugins" / "probe" / "data")


# ---------------------------------------------------------------------------
# Ready with work in the background; preparing; failed
# ---------------------------------------------------------------------------


def _health(f, value: str) -> None:
    r = f.client.put("/v1/plugins/probe/server-settings", json={"settings": {"health": value}}, headers=f.a)
    assert r.status_code == 200, r.text


def _places(f) -> dict:
    r = f.client.get("/v1/places", params={"q": "Probe"}, headers=f.a)
    assert r.status_code == 200, r.text
    return r.json()


@ONE_PYTHON
def test_part1_ready_with_background_work_answers_and_says_how_far(probe):
    f = probe
    _switch(f, f.a, True)
    _health(f, "busy")
    _until(f.runner, lambda: _running(f).get("detail") == "Fetching a newer list")
    running = _running(f)
    assert (running["state"], running["done"], running["total"]) == ("ready", 3, 10)
    found = _places(f)
    assert [p["place_id"] for p in found["places"]] == ["probe:lab"] and found["unavailable"] == []

    _health(f, "preparing")
    _until(f.runner, lambda: _running(f)["state"] == "preparing")
    assert (_running(f)["done"], _running(f)["total"], _running(f)["detail"]) == (1, 4, "Reading")
    found = _places(f)
    assert found["places"] == [] and [u["plugin_id"] for u in found["unavailable"]] == ["probe"]

    _health(f, "ready")
    _until(f.runner, lambda: _running(f)["state"] == "ready" and _running(f)["detail"] is None)


# ---------------------------------------------------------------------------
# An output's empty line and a credit's license
# ---------------------------------------------------------------------------


def _found(f, words: str) -> set[str]:
    r = f.client.get(f"/v1/query?f=query:{words}&f=library:{f.library_id}", headers=f.a)
    assert r.status_code == 200, r.text
    return {item["asset_id"] for item in r.json()["items"]}


@ONE_PYTHON
def test_part1_an_empty_line_shows_isnt_searched_and_the_license_is_beside_the_credit(probe):
    f = probe
    _switch(f, f.a, True)
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    acct = FakeAccount(scheduler_client(f.tenant_id))
    acct.tenant_id = f.tenant_id
    items = due(tenant_database_url(f.tenant_id), KIND, [f.library_id])
    assert sorted(c["asset_id"] for c in items) == sorted(f.clips)
    run_job(PluginWork, acct, f.tenant_id, KIND, items)
    a = f.clips[0]
    r = f.client.get(f"/v1/assets/{a}", headers=f.a)
    assert r.status_code == 200, r.text
    mine = [p for p in r.json()["facts"] if p["plugin_id"] == "probe"]
    assert len(mine) == 1 and (mine[0]["title"], mine[0]["credit"]) == ("Probe", CREDIT)
    seen, nothing = mine[0]["facts"]
    assert (seen["key"], seen["empty"]) == ("seen", False) and seen["text"] in ("Probed 1", "Probed 2")
    assert nothing == {"key": "nothing", "label": "Nothing", "text": "Nothing here zzqx", "url": None,
                       "empty": True}
    assert _found(f, "probed") == set(f.clips), "a searchable fact is found"
    assert _found(f, "zzqx") == set(), "an empty line is never searched"
    found = _places(f)
    assert [c for c in found["credits"] if c["source"] == "probe"] == [{"source": "probe", **CREDIT}]



# ---------------------------------------------------------------------------
# Part 2: the GeoNames program (src/own_plugins/geonames/), on the tester's own lists
# ---------------------------------------------------------------------------

GEONAMES = Path(__file__).resolve().parents[1] / "src" / "own_plugins" / "geonames"


class _Site:
    """A stand-in for download.geonames.org: these files, each request counted ("GET name", "HEAD name"); a file
    can be missing (404), or held half sent until let go."""

    def __init__(self, files: dict[str, bytes]) -> None:
        import collections
        import http.server

        self.files, self.counts = dict(files), collections.Counter()
        self.missing: set[str] = set()
        self.hold: str | None = None
        self.held, self.release = threading.Event(), threading.Event()
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a) -> None:
                pass

            def _head(self) -> bytes | None:
                name = self.path.lstrip("/")
                site.counts[f"{self.command} {name}"] += 1
                if name in site.missing or name not in site.files:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                body = site.files[name]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Last-Modified", "Mon, 19 Oct 2026 09:00:00 GMT")
                self.end_headers()
                return body

            def do_HEAD(self) -> None:  # noqa: N802 — http.server's name
                self._head()

            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                body = self._head()
                if body is None:
                    return
                if self.path.lstrip("/") == site.hold:
                    self.wfile.write(body[: len(body) // 2])
                    self.wfile.flush()
                    site.held.set()
                    site.release.wait(60)
                    return  # the connection ends short: a download cut off
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def gets(self) -> dict[str, int]:
        return {k[4:]: v for k, v in self.counts.items() if k.startswith("GET ")}

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()


@pytest.fixture
def lists_():
    from tests.tester_geonames_lists import lists

    return lists()


@pytest.fixture
def site(lists_):
    s = _Site(lists_.files())
    yield s
    s.close()


class _GeoNames:
    """The program installed as the runner sees it (a copy in its program folder, marked as an install marks it),
    on, with the site as its mirror, run by the real runner; asked through Lumiverb's own client."""

    def __init__(self, data: Path, mirror: str) -> None:
        import uuid

        from src.plugins.manifest import read
        from src.server.plugin_client import socket_path
        from src.server.plugin_install import INSTALL_MARK, program_dir
        from src.server.plugin_runner import Runner
        from src.server.plugin_settings import server_values

        program = program_dir(data, "geonames")
        shutil.copytree(GEONAMES, program)
        (program / INSTALL_MARK).write_text(uuid.uuid4().hex)
        self.manifest = read(GEONAMES)
        entry = SimpleNamespace(manifest=self.manifest)
        self.runner = Runner(data, installed=lambda: {"geonames": entry}, wanted=lambda found: set(found),
                             server_settings=lambda pid, m: server_values(m, {"mirror": mirror}))
        self.sock = socket_path(data, "geonames")
        self.data = data

    def said(self) -> dict:
        p = self.runner.plugins.get("geonames")
        return p.said() if p else {"state": "absent"}

    def until(self, state: str, within: float = 120, **more) -> dict:
        _until(self.runner, lambda: self.said()["state"] == state
               and all(self.said().get(k) == v for k, v in more.items()), within=within)
        return self.said()

    def ask(self, path: str, body: dict) -> dict:
        from src.server.plugin_client import ask

        return ask(self.sock, "POST", path, body=body, timeout=30)

    def kill(self) -> None:
        import signal

        os.killpg(self.runner.plugins["geonames"].process.pid, signal.SIGKILL)

    def stop(self) -> None:
        self.runner.stop_all()


def _settings(g: _GeoNames, radius_km: int, mirror: str) -> dict:
    from src.server.plugin_settings import values

    return values(g.manifest, {"radius_km": radius_km}, {"mirror": mirror})


@ONE_PYTHON
def test_part2_killed_its_restarted_and_uses_its_list_without_fetching_again(
        site, short_data, machine_python):  # noqa: F811
    g = _GeoNames(short_data, site.url)
    try:
        g.until("ready", detail=None)
        assert set(site.gets()) == {"cities1000.zip", "admin1CodesASCII.txt", "countryInfo.txt",
                                    "alternateNamesV2.zip", "allCountries.zip"}
        assert set(site.gets().values()) == {1}, f"each file fetched once: {site.gets()}"
        before = dict(site.counts)
        g.kill()
        g.until("ready", restarts=1)
        assert dict(site.counts) == before, "restarted with its list: nothing asked of the site"
        assert not (short_data / "plugins" / "geonames" / "data" / "building").exists()
    finally:
        g.stop()


@ONE_PYTHON
def test_part2_a_missing_file_fails_naming_it_and_its_ready_once_its_there(site, short_data, machine_python):  # noqa: F811
    site.missing = {"alternateNamesV2.zip"}
    g = _GeoNames(short_data, site.url)
    try:
        said = g.until("failing")
        assert "alternateNamesV2.zip" in (said["why"] or ""), said
        site.missing = set()
        g.kill()  # started again: it tries again at once
        g.until("ready", detail=None)
        assert site.gets().get("alternateNamesV2.zip") == 1
    finally:
        g.stop()


@ONE_PYTHON
def test_part2_cut_off_mid_download_it_keeps_what_it_finished(site, short_data, machine_python):  # noqa: F811
    """The last file (allCountries.zip) held half sent and the program killed: started again, the files it had
    finished aren't fetched again, the one cut off is, and the list is built."""
    site.hold = "allCountries.zip"
    g = _GeoNames(short_data, site.url)
    try:
        _until(g.runner, lambda: site.held.is_set(), within=60)
        g.kill()
        site.hold = None
        site.release.set()
        g.until("ready", detail=None, restarts=1)
        gets = site.gets()
        assert gets.pop("allCountries.zip") == 2, "the one cut off is fetched again"
        assert set(gets.values()) == {1}, f"each finished file fetched once: {gets}"
        part = list((short_data / "plugins" / "geonames" / "data").rglob("*.part"))
        assert part == [], f"no partial download left: {part}"
    finally:
        g.stop()



# ---------------------------------------------------------------------------
# Part 3: the switch-over
# ---------------------------------------------------------------------------


def test_part3_place_names_work_can_be_paused_and_resumed_on_the_machine(install, control, tmp_path):  # noqa: F811
    """`lumiverb-server pause|resume <producer>` (on the machine, no key) takes the GeoNames plug-in's work, as it
    took the built-in Place names' (place): the switch-over carries a pause of place over to plugin_geonames, so
    whoever paused it there must be able to resume it there. Run as the server admin runs it: a process of its
    own, which reads what's installed itself."""
    client, robert, tenant_id = install
    data = tmp_path / "data"
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={data}\n")
    command = Path(sys.executable).with_name("lumiverb-server")
    said = subprocess.run([str(command), "plugins", "install", str(GEONAMES), "--on", "--yes", "--env", str(env)],
                          capture_output=True, text=True, timeout=120)
    assert said.returncode == 0, said.stdout + said.stderr

    def pauses() -> set[tuple[str, str]]:
        with get_tenant_session(tenant_id) as session:
            return {tuple(r) for r in session.execute(text("SELECT target, scope FROM producer_pauses"))}

    for verb in ("pause", "resume"):
        said = subprocess.run([str(command), verb, "plugin_geonames", "--env", str(env)], capture_output=True, text=True,
                              timeout=120)
        assert said.returncode == 0, f"{verb}: {said.stdout}{said.stderr}"
        mine = {scope for target, scope in pauses() if target == "plugin_geonames"}
        assert ("work" in mine) if verb == "pause" else (mine == set()), f"after {verb}: {pauses()}"
