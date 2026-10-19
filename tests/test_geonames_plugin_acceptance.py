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
builder's), run by the real runner and asked through Lumiverb's own client:
its facts, searches and places by id equal what the built-in Place names
answered on the same files (but for a place at no real point, which it
leaves out: tests/plugins/tester_geonames_golden.json, written from the
built-in's code while it existed), every answer passing Lumiverb's checks;
killed, it's started again with its list and
fetches nothing; a file missing from the mirror fails naming it; a download
cut off is fetched again, the finished ones aren't.

Part 3, the switch-over: what the built-in's producer could be (paused and
resumed on the machine, `lumiverb-server pause|resume`), the plug-in's work
can be.

Part 4, the removal: a landmark's name finds its clips (At searched as Near
is), those named before the upgrade too; and an install that skipped part 3,
upgraded straight to the removal, loses nothing anyone made and is named
again once the plug-in is ready and its work resumed.
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
KIND = "plugin_prober"  # its id isn't probe: a word Lumiverb uses (ADR-024 phase 1)
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
    return _listed(f.client, f.a)["prober"]["running"]


def _started(f) -> dict | None:
    """What the probe last said it was started with, None before it ever started."""
    path = f.data / "plugins" / "prober" / "data" / "started.json"
    return json.loads(path.read_text()) if path.exists() else None


def _switch(f, who: dict, on: bool) -> None:
    r = f.client.put("/v1/plugins/prober", json={"enabled": on}, headers=who)
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
    assert f.runner.plugins["prober"].process is None

    _switch(f, f.b, True)  # the other account, not the one asking
    _until(f.runner, lambda: _running(f)["state"] == "ready")
    started = _started(f)
    assert started["python"] == f.python, "run by this machine's python3, not the test's"
    assert _listed(f.client, f.a)["prober"]["state"] == "off", "still off in this account"

    _switch(f, f.b, False)
    _until(f.runner, lambda: _running(f)["state"] == "off")
    assert f.runner.plugins["prober"].process is None

    # On by default: running though nobody chose; this account turning it off leaves the other following it.
    assert cli.main(["plugins", "default", "prober", "on", "--yes", "--env", str(f.env)]) == 0
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
    data_folder = f.data / "plugins" / "prober" / "data"
    assert Path(said["path"]).resolve().parent == (f.data / "plugins" / "prober").resolve()
    assert not Path(said["path"]).resolve().is_relative_to(data_folder.resolve()), "outside its data folder"
    assert said["mode"] == 0o600
    assert said["values"] == {"health": "ready", "mirror": "", "key": ""}, "the server's only, each its default"

    pid = started["pid"]
    r = f.client.put("/v1/plugins/prober/settings", json={"settings": {"word": "Changed"}}, headers=f.a)
    assert r.status_code == 200, r.text
    _turns(f, 20)
    assert _started(f)["pid"] == pid, "an account's setting comes with each request: no restart"

    r = f.client.put("/v1/plugins/prober/server-settings",
                     json={"settings": {"mirror": "http://127.0.0.1:1/lists/", "key": "s3cret"}}, headers=f.a)
    assert r.status_code == 200, r.text
    _until(f.runner, lambda: (_started(f) or {}).get("pid") != pid and _running(f)["state"] == "ready")
    again = _started(f)["server_settings"]
    assert again["values"] == {"health": "ready", "mirror": "http://127.0.0.1:1/lists/", "key": "s3cret"}
    assert again["mode"] == 0o600
    assert "s3cret" not in json.dumps(_listed(f.client, f.a)["prober"]), "a secret is never shown again"


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
    assert env["HOME"] == str(f.data / "plugins" / "prober" / "data")


# ---------------------------------------------------------------------------
# Ready with work in the background; preparing; failed
# ---------------------------------------------------------------------------


def _health(f, value: str) -> None:
    r = f.client.put("/v1/plugins/prober/server-settings", json={"settings": {"health": value}}, headers=f.a)
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
    assert [p["place_id"] for p in found["places"]] == ["prober:lab"] and found["unavailable"] == []

    _health(f, "preparing")
    _until(f.runner, lambda: _running(f)["state"] == "preparing")
    assert (_running(f)["done"], _running(f)["total"], _running(f)["detail"]) == (1, 4, "Reading")
    found = _places(f)
    assert found["places"] == [] and [u["plugin_id"] for u in found["unavailable"]] == ["prober"]

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
    mine = [p for p in r.json()["facts"] if p["plugin_id"] == "prober"]
    assert len(mine) == 1 and (mine[0]["title"], mine[0]["credit"]) == ("Probe", CREDIT)
    seen, nothing = mine[0]["facts"]
    assert (seen["key"], seen["empty"]) == ("seen", False) and seen["text"] in ("Probed 1", "Probed 2")
    assert nothing == {"key": "nothing", "label": "Nothing", "text": "Nothing here zzqx", "url": None,
                       "empty": True}
    assert _found(f, "probed") == set(f.clips), "a searchable fact is found"
    assert _found(f, "zzqx") == set(), "an empty line is never searched"
    found = _places(f)
    assert [c for c in found["credits"] if c["source"] == "prober"] == [{"source": "prober", **CREDIT}]



# ---------------------------------------------------------------------------
# Part 2: the GeoNames program (src/own_plugins/geonames/), against the built-in's answers on the tester's lists
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


# What the built-in Place names answered on the tester's lists, written from its code while it existed (part 3's
# head, v0.1.103); its "about" says how it's laid out.
TESTER_GOLDEN = Path(__file__).parent / "plugins" / "tester_geonames_golden.json"


def _sha(value) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def _files_sha(files: dict[str, bytes]) -> dict[str, str]:
    """Each file's SHA-256, a zip's of what it holds (its own bytes carry the time it was made)."""
    import hashlib
    import io
    import zipfile

    out = {}
    for name, body in sorted(files.items()):
        h = hashlib.sha256()
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(body)) as z:
                for member in sorted(z.namelist()):
                    h.update(member.encode() + b"\0" + z.read(member))
        else:
            h.update(body)
        out[name] = h.hexdigest()
    return out


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


def test_part2_the_program_answers_as_the_built_in_did_on_the_testers_lists(site, lists_, short_data, machine_python):  # noqa: F811
    """Its facts (at, near) for every point at radii of 1, 5 and 25 km, its searches and its places by id, from
    its own socket, against what the built-in Place names answered on the same files (the tester's golden file,
    written from its code while it existed): equal to the character, and every answer passes Lumiverb's own
    checks (manifest.checked_outputs, plugin_lookups.checked_place)."""
    from src.plugins.manifest import checked_outputs
    from src.server.plugin_lookups import checked_place

    golden = json.loads(TESTER_GOLDEN.read_text())
    points, queries = lists_.points(), lists_.queries()
    asked = {"files": _files_sha(lists_.files()), "points": _sha(points), "queries": _sha(queries)}
    assert asked == {k: golden[k] for k in asked}, "the golden file was written on these lists and questions"
    names = golden["names"]
    g = _GeoNames(short_data, site.url)
    try:
        g.until("ready", detail=None)
        plugin = SimpleNamespace(manifest=g.manifest)
        differences = []
        for km in (1, 5, 25):
            for start in range(0, len(points), 200):
                batch = points[start:start + 200]
                clips = [{"id": str(n), "location": {"lat": la, "lon": lo, "sure_m": sure, "source": "file"}}
                         for n, (la, lo, sure) in enumerate(batch, 1)]
                said = g.ask("/v1/work", {"settings": _settings(g, km, site.url), "clips": clips})["clips"]
                for n, (la, lo, sure) in enumerate(batch, 1):
                    outputs = said[str(n)]["outputs"]
                    checked_outputs(g.manifest.work, outputs)  # raises: what Lumiverb would refuse
                    at, near = golden["work"][str(km)][start + n - 1]
                    want = {"at": names[at] if at >= 0 else None, "near": names[near] if near >= 0 else None}
                    got = {k: (v or {}).get("text") for k, v in outputs.items()}
                    if got != want:
                        differences.append(("work", km, la, lo, sure, want, got))
        fields = ("name", "region", "country", "kind", "matched", "population", "lat", "lon")
        for q, answer in zip(queries, golden["search"], strict=True):
            got = g.ask("/v1/lookups/places", {"q": q, "limit": 10, "settings": _settings(g, 25, site.url)})["places"]
            for raw in got:
                checked_place(plugin, raw)
            # The built-in's, but for a place at no real point, which the plug-in leaves out (plan section 1: what
            # it keeps passes Lumiverb's checks, plugin_lookups.checked_place); the golden file has left them out.
            want = []
            for one in answer:
                gid, matched = (one, None) if isinstance(one, str) else one
                want.append({"id": gid, **golden["places"][gid], "matched": matched})
            mine = [{"id": p["id"], **{k: p.get(k) for k in fields}} for p in got]
            if mine != want:
                differences.append(("search", q, want, mine))
        for gid, want in golden["ids"]:
            got = g.ask("/v1/lookups/places", {"id": str(gid), "settings": _settings(g, 25, site.url)})["places"]
            if want is None:
                if got:
                    differences.append(("id", gid, None, got))
            elif [(p["id"], p["name"], p["lat"], p["lon"], p["kind"]) for p in got] != [
                    (str(gid), want["name"], want["lat"], want["lon"], want["kind"])]:
                differences.append(("id", gid, want, got))
        assert differences == [], f"{len(differences)} differences, the first: {differences[:3]}"
    finally:
        g.stop()


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


# ---------------------------------------------------------------------------
# Part 4: the removal; a landmark's name is searched, for the clips named before it too (Robert's Q2)
# ---------------------------------------------------------------------------

QUILLSTONE = "Quillstone Abbey"  # a church (CH, 250 m) with a Wikipedia link, among Paris's made-up towns
BRINDLEMOOR = "Brindlemoor Peak"  # a mountain (MT, 1 km) with a link, far from any town: no town near it


@pytest.fixture
def landmark_lists():
    """The tester's lists with two landmarks of their own, whose names are in no town's: (lists, {name: point})."""
    from tests.tester_geonames_lists import _offset, lists

    made = lists()
    gid = max(row[0] for row in made.towns + made.landmarks)
    aid = max(row[0] for row in made.names)
    points = {}
    for name, code, (lat0, lon0), meters in ((QUILLSTONE, "CH", (48.85, 2.35), 3000),
                                            (BRINDLEMOOR, "MT", (-77.85, 166.67), 150_000)):
        gid, aid = gid + 1, aid + 1
        lat, lon = (round(v, 6) for v in _offset(lat0, lon0, meters, 1.0))
        made.landmarks.append((gid, name, name, lat, lon, code, "FR" if code == "CH" else "AQ",
                               "11" if code == "CH" else "00", True))
        made.names.append((aid, gid, "link", f"https://en.wikipedia.org/wiki/L{gid}", "", ""))
        points[name] = (lat, lon)
    return made, points


def _part3s(folder: Path) -> Path:
    """GeoNames as v0.1.103 shipped it, for an install that named its clips then: the program as shipped now,
    its manifest as part 3's (its version then; At not searchable)."""
    from src.plugins.manifest import MANIFEST

    old = folder / "geonames"
    shutil.copytree(GEONAMES, old)
    manifest = json.loads((old / MANIFEST).read_text())
    manifest["version"] = "1"
    for output in manifest["work"]["outputs"]:
        if output["key"] == "at":
            output.pop("searchable", None)
    (old / MANIFEST).write_text(json.dumps(manifest, indent=2))
    return old


def _sweep(client) -> int:
    """Upkeep's search sweep, as its timer asks it (every account, the install's key): the facts written again."""
    r = client.post("/v1/upkeep/search-sync?tenants=all", headers={"Authorization": f"Bearer {INSTALL_KEY}"})
    assert r.status_code == 200, r.text
    assert r.json()["failed"] == 0, r.json()
    return r.json()["facts_synced"]


def test_part4_a_landmarks_name_finds_its_clips_named_before_the_upgrade_and_after(
        install, control, short_data, landmark_lists, tmp_path, monkeypatch):  # noqa: F811
    """Robert: people search by landmarks more than towns, so At is searched as Near is. An install that named
    its clips under part 3 (At not searched) and is upgraded as an install is (`lumiverb-server migrate`, a
    process of its own: migrations, then Lumiverb's own plug-ins updated; the runner started again; whatever
    work is due then run; upkeep's search sweep, every 5 minutes) finds those clips by their landmark's name,
    with what each shows kept throughout and nothing made again; and a clip named after the upgrade too. A
    landmark with no town near (its fact its label) too."""
    from src.server import own_plugins
    from src.server.plugin_runner import Runner

    client, robert, tenant_id = install
    made, points = landmark_lists
    site = _Site(made.files())
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\nLUMIVERB_GEONAMES_URL={site.url}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    shipped_at = own_plugins.OWN
    monkeypatch.setattr(own_plugins, "OWN", _part3s(tmp_path / "v0.1.103").parent)
    own_plugins.shipped.cache_clear()
    own_plugins.install_own(control, short_data, environ={"LUMIVERB_GEONAMES_URL": site.url}, say=lambda line: None)
    refresh()
    r = client.put("/v1/plugins/geonames", json={"enabled": True, "acknowledged": "3"}, headers=robert)
    assert r.status_code == 200, r.text
    r = client.post("/v1/libraries", json={"name": "Landmarks", "root_path": "/Volumes/landmarks"}, headers=robert)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    acct = FakeAccount(scheduler_client(tenant_id))
    acct.tenant_id = tenant_id

    def clips_at(name: str, *files: str) -> list[str]:
        lat, lon = points[name]
        with get_tenant_session(tenant_id) as session:
            made_ = [make_clip(session, library_id, f) for f in files]
            session.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = ANY(:ids)"),
                            {"lat": lat, "lon": lon, "ids": made_})
            session.commit()
        return made_

    def name_whats_due() -> None:
        items = due(tenant_database_url(tenant_id), "plugin_geonames", [library_id])
        if items:
            run_job(PluginWork, acct, tenant_id, "plugin_geonames", items)
        assert acct.failures.charged == []

    def shown(clip: str) -> dict:
        r = client.get(f"/v1/assets/{clip}", headers=robert)
        assert r.status_code == 200, r.text
        section = next(p for p in r.json()["facts"] if p["plugin_id"] == "geonames")
        return {f["key"]: f["text"] for f in section["facts"]}

    def found(words: str) -> set[str]:
        r = client.get(f"/v1/query?f=query:{words}&f=library:{library_id}", headers=robert)
        assert r.status_code == 200, r.text
        return {item["asset_id"] for item in r.json()["items"]}

    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["geonames"]["running"]["state"] == "ready", within=120)
        before = clips_at(QUILLSTONE, "q1.jpg", "q2.jpg") + clips_at(BRINDLEMOOR, "b1.jpg")
        name_whats_due()
        then = {clip: shown(clip) for clip in before}
        assert [then[c]["at"] for c in before[:2]] == [QUILLSTONE] * 2, then
        assert then[before[2]]["at"].startswith(f"{BRINDLEMOOR}, ") and then[before[2]]["near"] == "Not near a town"
        assert found("Quillstone") == found("Brindlemoor") == set(), "part 3: At isn't searched"
        near = then[before[0]]["near"].split(",")[0]
        assert set(before[:2]) <= found(near), "Near is"
    finally:
        runner.stop_all()  # the upgrade: the services stopped

    monkeypatch.setattr(own_plugins, "OWN", shipped_at)
    own_plugins.shipped.cache_clear()
    said = subprocess.run([str(Path(sys.executable).with_name("lumiverb-server")), "migrate", "--env", str(env)],
                          capture_output=True, text=True, timeout=300)
    assert said.returncode == 0, said.stdout + said.stderr
    assert {clip: shown(clip) for clip in before} == then, "what each shows is kept through the upgrade"
    refresh()
    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["geonames"]["running"]["state"] == "ready", within=120)
        assert {clip: shown(clip) for clip in before} == then, "and while the plug-in starts again"
        name_whats_due()
        assert {clip: shown(clip) for clip in before} == then, "and once what's due is done"
        assert _sweep(client) == 3, "the sweep writes the search text of the facts saved before"
        assert _sweep(client) == 0, "once"
        assert {clip: shown(clip) for clip in before} == then, "and after it"
        assert found("Quillstone") == set(before[:2]), "the clips named before the upgrade are found by it"
        assert found("Brindlemoor") == {before[2]}, "a landmark with no town near too"
        assert found(QUILLSTONE.replace(" ", "%20")) == set(before[:2])
        after = clips_at(QUILLSTONE, "q3.jpg")
        name_whats_due()
        assert shown(after[0]) == then[before[0]]
        assert found("Quillstone") == set(before[:2] + after), "and one named after it"
        assert set(before[:2] + after) <= found(near), "Near is still searched"
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)
        site.close()


# ---------------------------------------------------------------------------
# Part 4: an install that skipped part 3, upgraded straight to the removal (Robert's Q1: the gap is accepted)
# ---------------------------------------------------------------------------

BEFORE_PART3 = {"control": ("alembic-control.ini", "ALEMBIC_CONTROL_URL", "ebf45b3545aa"),  # v0.1.102's heads
                "tenant": ("alembic-tenant.ini", "ALEMBIC_TENANT_URL", "552e1e291a91")}
GEONAMES_CREDIT = {"text": "GeoNames", "url": "https://www.geonames.org/",
                   "license": {"text": "CC BY 4.0", "url": "https://creativecommons.org/licenses/by/4.0/"}}


def _alembic_to(tree: str, url: str, verb: str, revision: str) -> None:
    ini, var, _ = BEFORE_PART3[tree]
    said = subprocess.run([sys.executable, "-m", "alembic", "-c", ini, verb, revision],
                          cwd=Path(__file__).resolve().parents[1], env={**os.environ, var: url},
                          capture_output=True, text=True, timeout=300)
    assert said.returncode == 0, said.stdout + said.stderr


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _between(made, low_km: float, high_km: float) -> tuple[float, float]:
    """A point whose nearest town (one GeoNames names) is between low_km and high_km away and no landmark within
    5 km: Near a town at a 40 km radius, at 25 none."""
    import math

    from tests.tester_geonames_lists import NOT_A_TOWN, _offset

    towns = [(la, lo) for _g, _n, _a, la, lo, code, *_ in made.towns
             if code not in NOT_A_TOWN and -90 <= la <= 90 and -180 <= lo <= 180 and (la, lo) != (0.0, 0.0)]
    marks = [(la, lo) for _g, _n, _a, la, lo, *_ in made.landmarks]
    for town in towns:
        for bearing in range(0, 360, 15):
            point = _offset(*town, (low_km + high_km) / 2 * 1000, math.radians(bearing))
            if low_km < min(_km(point, t) for t in towns) < high_km and min(_km(point, m) for m in marks) > 5:
                return round(point[0], 6), round(point[1], 6)
    raise AssertionError("no such point in the tester's lists")


def test_part4_an_account_from_before_part_3_upgraded_straight_to_head_loses_nothing_and_is_named_again(
        install, control, short_data, landmark_lists, tmp_path, monkeypatch):  # noqa: F811
    """An install on v0.1.102 (before the switch-over) updated straight to the removal, as a Linux install
    updating after a while is: its databases as v0.1.102 left them (taken back to its revisions, then seeded as it
    left an account with GeoNames on: the built-in's names, its list, its radius of 40 km chosen, its producer's
    work and redo paused, a person's location set from one of GeoNames' places), then `lumiverb-server migrate` (a
    process of its own: every migration from there, in order, then Lumiverb's own plug-ins). Nothing anyone made
    is lost: the radius is the plug-in's, the pauses its work's, the person's place kept with GeoNames' credit and
    license. Until the plug-in names them the clips show no name (the gap Robert accepted) and Place search says
    why; once it's ready and the work resumed, every located clip is named again (at 40 km), found by its
    landmark's name too."""
    from src.server.database import get_control_session
    from src.server.plugin_runner import Runner
    from src.server.repository.plugins import PluginRepository
    from src.shared.utils import utcnow

    client, robert, tenant_id = install
    made, points = landmark_lists
    far = _between(made, 28, 37)
    quill = next(row for row in made.landmarks if row[1] == QUILLSTONE)
    site = _Site(made.files())
    env = _env_file(tmp_path, control)
    env.write_text(env.read_text() + f"DATA_DIR={short_data}\nLUMIVERB_GEONAMES_URL={site.url}\n")
    monkeypatch.setenv("DATA_DIR", str(short_data))
    get_settings.cache_clear()
    command = Path(sys.executable).with_name("lumiverb-server")
    account = tenant_database_url(tenant_id)
    r = client.post("/v1/libraries", json={"name": "Old", "root_path": "/Volumes/old"}, headers=robert)
    assert r.status_code == 200, r.text
    library_id = r.json()["library_id"]
    spots = {"q1": points[QUILLSTONE], "q2": points[QUILLSTONE], "b1": points[BRINDLEMOOR], "far": far,
             "picked": points[QUILLSTONE]}
    with get_tenant_session(tenant_id) as session:
        clips = {name: make_clip(session, library_id, f"{name}.jpg") for name in spots}
        for name, (lat, lon) in spots.items():
            session.execute(text("UPDATE assets SET gps_lat = :lat, gps_lon = :lon WHERE asset_id = :a"),
                            {"lat": lat, "lon": lon, "a": clips[name]})
        PluginRepository(session).set("geonames", True, acknowledged_version="3", by="usr_robert", now=utcnow())
        session.commit()

    # v0.1.102's databases, as it left them.
    _alembic_to("tenant", account, "downgrade", BEFORE_PART3["tenant"][2])
    _alembic_to("control", control, "downgrade", BEFORE_PART3["control"][2])
    picked = {"id": f"geonames:{quill[0]}", "label": f"{QUILLSTONE}, Île-de-France, France", "source": "geonames"}
    engine = create_engine(account)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.place', :v, now())"),
                     {"v": json.dumps({"radius_km": 40})})
        for scope in ("work", "redo"):
            conn.execute(text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                              " VALUES ('place', :s, 'usr_robert', now())"), {"s": scope})
        for name in ("q1", "q2", "b1", "far"):
            conn.execute(text(
                "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                "   produced_at, outcome, attempts) VALUES (:a, 'place', 'place', '2', 'h', now(), 'ok', 0)"),
                {"a": clips[name]})
            lat, lon = spots[name]
            conn.execute(text(
                "INSERT INTO asset_place (asset_id, place_id, label, country_code, distance_m, lat, lon, named_at)"
                " VALUES (:a, 'geonames:1', 'The old name', 'FR', 100, :lat, :lon, now())"),
                {"a": clips[name], "lat": lat, "lon": lon})
        conn.execute(text(
            "INSERT INTO asset_location (asset_id, lat, lon, radius_m, source, status, basis, set_by, set_at)"
            " VALUES (:a, :lat, :lon, 0, 'person', 'applied', CAST(:b AS jsonb), 'usr_robert', now())"),
            {"a": clips["picked"], "lat": spots["picked"][0], "lon": spots["picked"][1],
             "b": json.dumps({"place": picked})})
    with create_engine(control).begin() as conn:
        conn.execute(text("INSERT INTO plugin_data (plugin_id, state, done, count, version, updated_at)"
                          " VALUES ('geonames', 'ready', 1, 1, 2, now())"))

    # As on the install: its env file names the mirror (the suite's own stand-in, in this process's environment,
    # would win over the file).
    install_environ = {k: v for k, v in os.environ.items() if k != "LUMIVERB_GEONAMES_URL"}
    said = subprocess.run([str(command), "migrate", "--env", str(env)], capture_output=True, text=True, timeout=300,
                          env=install_environ)
    assert said.returncode == 0, said.stdout + said.stderr
    assert f"its mirror: {site.url} (LUMIVERB_GEONAMES_URL)" in said.stdout, said.stdout

    # Nothing anyone made is lost; what the built-in left is gone.
    with engine.connect() as conn:
        tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        assert "asset_place" not in tables
        assert conn.execute(text("SELECT chosen FROM plugin_settings WHERE plugin_id = 'geonames'")).scalar() == {
            "radius_km": 40}
        assert sorted(conn.execute(text("SELECT target, scope FROM producer_pauses")).all()) == [
            ("plugin_geonames", "redo"), ("plugin_geonames", "work")]
        assert conn.execute(text("SELECT count(*) FROM artifact_lineage WHERE artifact = 'place'")).scalar() == 0
        row = conn.execute(text("SELECT lat, lon, source, basis FROM asset_location WHERE asset_id = :a"),
                           {"a": clips["picked"]}).one()
    engine.dispose()
    assert (row.lat, row.lon, row.source) == (*spots["picked"], "person")
    assert row.basis == {"place": {**picked, "credit": GEONAMES_CREDIT}}, "the person's place, credited"
    with get_control_session() as session:
        tables = set(session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
        assert not {t for t in tables if t.startswith("geonames_")} | ({"plugin_data"} & tables)
        installed = session.execute(text("SELECT version, installed_by FROM installed_plugins"
                                         " WHERE plugin_id = 'geonames'")).one()
        assert tuple(installed) == ("2", "lumiverb")
    refresh()

    def shown(clip: str) -> dict:
        r = client.get(f"/v1/assets/{clip}", headers=robert)
        assert r.status_code == 200, r.text
        assert "place" not in r.json(), "no old name anywhere"
        return {f["key"]: f["text"] for p in r.json()["facts"] if p["plugin_id"] == "geonames" for f in p["facts"]}

    def found(words: str) -> set[str]:
        r = client.get(f"/v1/query?f=query:{words}&f=library:{library_id}", headers=robert)
        assert r.status_code == 200, r.text
        return {item["asset_id"] for item in r.json()["items"]}

    assert all(shown(c) == {} for c in clips.values()), "the gap: no names until the plug-in's"
    r = client.get(f"/v1/assets/{clips['picked']}", headers=robert)
    assert (r.json()["location"]["basis_summary"], r.json()["location"]["credit"]) == (picked["label"],
                                                                                      GEONAMES_CREDIT)
    r = client.get("/v1/places", params={"q": "Quillstone"}, headers=robert)
    assert r.status_code == 200 and r.json()["places"] == [], r.text
    assert [u["plugin_id"] for u in r.json()["unavailable"]] == ["geonames"], "Place search says why"

    runner = Runner(short_data)
    try:
        _until(runner, lambda: _listed(client, robert)["geonames"]["running"]["state"] == "ready", within=120)
        r = client.get("/v1/places", params={"q": "Quillstone"}, headers=robert)
        assert [p["place_id"] for p in r.json()["places"]] == [picked["id"]], r.text
        rows = client.get("/v1/producers", headers=robert).json()
        mine = next(p for p in (rows.get("producers", rows) if isinstance(rows, dict) else rows)
                    if p["artifact"] == "plugin_geonames")
        assert mine["paused"] is True, "its work paused, as Place names' was: nothing named until resumed"
        assert all(shown(c) == {} for c in clips.values())

        resumed = subprocess.run([str(command), "resume", "plugin_geonames", "--env", str(env)],
                                 capture_output=True, text=True, timeout=120)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        acct = FakeAccount(scheduler_client(tenant_id))
        acct.tenant_id = tenant_id
        items = due(account, "plugin_geonames", [library_id])
        assert {c["asset_id"] for c in items} == set(clips.values()), "every located clip is due"
        run_job(PluginWork, acct, tenant_id, "plugin_geonames", items)
        assert acct.failures.charged == []
        named = {name: shown(clip) for name, clip in clips.items()}
        assert [named[n]["at"] for n in ("q1", "q2", "picked")] == [QUILLSTONE] * 3, named
        assert named["b1"]["at"].startswith(f"{BRINDLEMOOR}, ") and named["b1"]["near"] == "Not near a town", named
        assert named["far"]["near"] != "Not near a town" and "at" not in named["far"], \
            f"its town is 28 to 37 km away: named at the 40 km chosen before, not 25 ({named['far']})"
        assert found("Quillstone") == {clips["q1"], clips["q2"], clips["picked"]}
        assert found("Brindlemoor") == {clips["b1"]}
    finally:
        runner.stop_all()
        use_added((), by=INSTALLED)
        site.close()
