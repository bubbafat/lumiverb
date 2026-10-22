"""Each part gets only the secrets it needs (src/shared/secrets_env.py).

The install's env file holds its keys and passwords. run gives each part
only its own (test_server_run.py); on Linux the plug-in runner starts again
with only its own; the scheduler, once its settings are read, drops its own
from its environment, so the tools it starts (ffmpeg, exiftool, its face and
transcript processes) have none; and the API's tools get an environment
without them. A plug-in runs as their user: on Linux each of them makes
itself not dumpable as it starts, so it can't read their environment or
memory.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.shared import secrets_env

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]

DB = "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/control_plane"
SECRETS = {"CONTROL_PLANE_DATABASE_URL": DB, "TENANT_DATABASE_URL_TEMPLATE": DB.replace("control_plane", "{tenant_id}"),
           "ADMIN_KEY": "a", "API_SECRET_KEY": "b", "JWT_SECRET": "c", "SMTP_PASSWORD": "d", "PGPASSWORD": "e",
           "SOME_SERVICE_TOKEN": "f", "ANOTHER_DB": "mysql://u:p@h/db"}
NOT_SECRETS = {"PATH": "/usr/bin", "HOME": "/Users/x", "DATA_DIR": "/data", "QUICKWIT_URL": "http://127.0.0.1:6466",
               "TOKENIZERS_PARALLELISM": "false", "LUMIVERB_ROOT_MAP": '{"/": "/"}', "KEYCHAIN_PATH": "/k",
               "APP_HOST": "http://localhost:6464", "PG_BIN": "/opt/homebrew/opt/postgresql@18/bin"}


@pytest.fixture
def environment():
    """The process's environment, put back whatever the test did to it."""
    saved = dict(os.environ)
    yield os.environ
    os.environ.clear()
    os.environ.update(saved)


def test_secrets_are_told_by_name_or_by_a_password_in_a_url():
    assert [n for n, v in {**SECRETS, **NOT_SECRETS}.items() if secrets_env.is_secret(n, v)] == list(SECRETS)


def test_without_secrets_keeps_only_what_it_is_told_to():
    env = {**SECRETS, **NOT_SECRETS}
    assert secrets_env.without_secrets(env) == NOT_SECRETS
    assert secrets_env.without_secrets(env, keep=["CONTROL_PLANE_DATABASE_URL"]) == {
        **NOT_SECRETS, "CONTROL_PLANE_DATABASE_URL": DB}


def test_what_a_process_starts_after_forgetting_has_no_secrets(environment):
    environment.update(SECRETS)
    environment.update(NOT_SECRETS)
    dropped = secrets_env.forget()
    assert set(SECRETS) <= set(dropped)
    seen = json.loads(subprocess.run([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                                     capture_output=True, text=True, check=True).stdout)
    assert not set(SECRETS) & set(seen)
    assert seen["DATA_DIR"] == "/data" and seen["TOKENIZERS_PARALLELISM"] == "false"


def test_the_scheduler_forgets_its_secrets_once_its_settings_are_read(environment, monkeypatch, tmp_path):
    from src.server.config import get_settings
    from src.server.scheduler import service

    class NotFurther:  # the next step: stops it there
        def acquire(self) -> bool:
            return False

    environment.update(SECRETS)
    monkeypatch.chdir(tmp_path)  # no .env of this checkout's
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(service, "configure_logging", lambda: None)
    monkeypatch.setattr("src.server.database.APPLICATION_NAME", "test")
    monkeypatch.setattr("src.server.scheduler.scans.ServiceLock", NotFurther)
    get_settings.cache_clear()
    try:
        assert service.main() == 1
        assert not set(SECRETS) & set(os.environ), "what it starts would have them"
        assert get_settings().control_plane_database_url == DB, "it read them first"
    finally:
        get_settings.cache_clear()


def test_the_apis_playback_tools_have_no_secrets(environment, monkeypatch, tmp_path):
    from src.server.api.routers import playback

    environment.update(SECRETS)
    given = []

    def run(cmd, **kwargs):
        given.append(kwargs.get("env"))
        raise subprocess.SubprocessError("not run")

    monkeypatch.setattr(playback.subprocess, "run", run)
    assert playback._duration(tmp_path / "x.mp4") is None
    assert given and given[0] is not None and not set(SECRETS) & set(given[0])


class _StartedError(Exception):
    """What a stand-in for a program raises once it has seen its environment."""


def _what_it_starts_with(monkeypatch, module, name: str = "run") -> list[dict]:
    """`module.subprocess.<name>` replaced: each call's environment noted, then _StartedError."""
    given: list[dict] = []

    def start(cmd, **kwargs):
        given.append(kwargs.get("env"))
        raise _StartedError

    monkeypatch.setattr(module.subprocess, name, start)
    return given


def test_none_of_the_apis_other_tools_has_its_secrets(environment, monkeypatch, tmp_path):
    """The API keeps every secret (it signs sessions, opens sealed keys), and what it starts would start with them,
    readable by every process of its user while it runs (a plug-in's among them): ffprobe and ffmpeg on an upload,
    git for its version, journalctl for Admin › Logs, a new account's migrations (its database's URL alone) and a
    backup's pg_dump (its password alone)."""
    from src.processing.video import ffmpeg_run
    from src.server import backups, database, service_log, uploaded_video, version

    environment.update(SECRETS)
    environment.update(NOT_SECRETS)
    started: list[dict] = []
    for module, name, call in (
            (uploaded_video, "run", lambda: uploaded_video.probe(tmp_path / "x.mp4")),
            (ffmpeg_run, "Popen", lambda: ffmpeg_run.run(["ffmpeg", "-i", "x"])),
            (ffmpeg_run, "run", lambda: ffmpeg_run._opens.__wrapped__("vulkan")),
            (version, "run", lambda: version.running.__wrapped__())):
        given = _what_it_starts_with(monkeypatch, module, name)
        with contextlib.suppress(_StartedError):
            call()
        assert len(given) == 1 and given[0] is not None, module.__name__
        started += given
    journal: list[dict] = []
    service_log._JournalReader(None, run=lambda argv, **kwargs: journal.append(kwargs.get("env")) or SimpleNamespace(
        stdout="", stderr="", returncode=0))._batch(None)
    started += journal
    for env in started:
        assert not set(SECRETS) & set(env) and env["DATA_DIR"] == "/data"

    _, dump = backups._libpq(DB)
    assert not (set(SECRETS) - {"PGPASSWORD"}) & set(dump) and dump["PGPASSWORD"] == "pw"

    class Engine:
        def connect(self):
            return contextlib.nullcontext(SimpleNamespace(execute=lambda *a: None, commit=lambda: None))

        def dispose(self) -> None:
            pass

    monkeypatch.setattr(database, "create_engine", lambda *a, **k: Engine())
    monkeypatch.setattr(database, "get_settings", lambda: SimpleNamespace(
        tenant_database_url_template=SECRETS["TENANT_DATABASE_URL_TEMPLATE"]))
    migrations = _what_it_starts_with(monkeypatch, database)
    with pytest.raises(_StartedError):
        database.provision_tenant_database("t_1")
    assert not set(SECRETS) & set(migrations[0])
    assert migrations[0]["ALEMBIC_TENANT_URL"] == DB.replace("control_plane", "t_1")


def _python(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """`code` in a Python of its own, this checkout first on its path."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False,
                          env={**(env if env is not None else os.environ), "PYTHONPATH": str(REPO)})


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux's: elsewhere nothing changes")
def test_on_linux_a_process_that_calls_it_is_no_longer_dumpable():
    out = _python("import ctypes; from src.shared import secrets_env\n"
                  "libc = ctypes.CDLL(None)\n"
                  "print(libc.prctl(secrets_env.PR_GET_DUMPABLE, 0, 0, 0, 0), secrets_env.not_dumpable(),"
                  " libc.prctl(secrets_env.PR_GET_DUMPABLE, 0, 0, 0, 0))")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1", "True", "0"], "dumpable before, not after"


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="Linux's own test is above")
def test_elsewhere_it_changes_nothing():
    out = _python("from src.shared import secrets_env; print(secrets_env.not_dumpable())")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False"]


def test_keep_only_starts_the_same_process_again_without_the_other_secrets():
    # Deleting them from os.environ isn't enough: the environment a process started with stays in its memory
    # (/proc/<pid>/environ on Linux).
    code = ("import json, os, sys; from src.shared import secrets_env\n"
            "print('pid', os.getpid(), flush=True)\n"
            "secrets_env.keep_only(['CONTROL_PLANE_DATABASE_URL'])\n"
            "started = open('/proc/self/environ', 'rb').read().decode() if sys.platform.startswith('linux') else ''\n"
            "print(json.dumps({'env': dict(os.environ), 'started': started}))")
    out = _python(code, {**SECRETS, **NOT_SECRETS})
    assert out.returncode == 0, out.stderr
    *pids, said = out.stdout.splitlines()
    assert len(pids) == 2 and pids[0] == pids[1], "started again in place: the same process"
    seen = json.loads(said)
    assert seen["env"]["CONTROL_PLANE_DATABASE_URL"] == DB and seen["env"]["DATA_DIR"] == "/data"
    assert not (set(SECRETS) - {"CONTROL_PLANE_DATABASE_URL"}) & set(seen["env"])
    assert "JWT_SECRET" not in seen["started"] and "ADMIN_KEY" not in seen["started"]
    out = _python(code, {**NOT_SECRETS})  # nothing to drop: not started again
    assert out.returncode == 0 and len(out.stdout.splitlines()) == 2, out.stderr


def test_the_apis_processes_make_themselves_not_dumpable_first():
    # Each worker uvicorn starts imports the app; uvicorn's own process is python -m src.server.api.
    out = _python("from src.shared import secrets_env\n"
                  "calls = []\n"
                  "secrets_env.not_dumpable = lambda: calls.append('not_dumpable') or True\n"
                  "import src.server.api.main\n"
                  "print(calls)")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[-2] == "['not_dumpable']"
    # uvicorn's own process: not dumpable first; multiprocessing's resource tracker, the program it keeps beside the
    # workers, started without its secrets; the workers told to forget theirs; then uvicorn as it was asked.
    out = _python("import os, runpy, sys\n"
                  "from multiprocessing import resource_tracker\n"
                  "from src.shared import secrets_env\n"
                  "import uvicorn.main\n"
                  "order = []\n"
                  "secrets_env.not_dumpable = lambda: order.append('not_dumpable') or True\n"
                  "resource_tracker.ensure_running = lambda: order.append(\n"
                  "    'tracker ' + ('with secrets' if 'JWT_SECRET' in os.environ else 'without secrets'))\n"
                  "uvicorn.main.main = lambda prog_name: order.append(\n"
                  "    f'{prog_name} {sys.argv[1:]} {os.environ[secrets_env.API_FORGETS]} {os.environ[\"JWT_SECRET\"]}')\n"
                  "sys.argv = ['x', 'src.server.api.main:app', '--workers', '2']\n"
                  "runpy.run_module('src.server.api', run_name='__main__', alter_sys=True)\n"
                  "print(order)", {**os.environ, **SECRETS})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split("\n")[-2] == (
        "['not_dumpable', 'tracker without secrets', \"uvicorn ['src.server.api.main:app', '--workers', '2'] 1 c\"]")


def test_a_served_api_worker_forgets_its_secrets_once_its_settings_are_read():
    # As the scheduler does: what it starts (its face process, which lives on) has none. Only when served
    # (python -m src.server.api says so): a test importing the app keeps its environment.
    code = ("import json, os\n"
            "import src.server.api.main\n"
            "from src.server.config import get_settings\n"
            "s = get_settings()\n"
            "print(json.dumps({'env': sorted(os.environ), 'jwt': s.jwt_secret, 'db': s.control_plane_database_url}))")
    served = _python(code, {**os.environ, **SECRETS, secrets_env.API_FORGETS: "1"})
    assert served.returncode == 0, served.stderr
    seen = json.loads(served.stdout.splitlines()[-1])
    assert not set(SECRETS) & set(seen["env"]) and seen["jwt"] == "c" and seen["db"] == DB
    imported = _python(code, {**{k: v for k, v in os.environ.items() if k != secrets_env.API_FORGETS}, **SECRETS})
    assert imported.returncode == 0, imported.stderr
    assert set(SECRETS) <= set(json.loads(imported.stdout.splitlines()[-1])["env"])


def test_what_starts_while_they_are_out_has_no_secrets_and_they_come_back(environment):
    environment.update(SECRETS)
    environment.update(NOT_SECRETS)
    seen = secrets_env.started_without_secrets(lambda: json.loads(subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"], capture_output=True, text=True,
        check=True).stdout))
    assert not set(SECRETS) & set(seen) and seen["DATA_DIR"] == "/data"
    assert {name: os.environ[name] for name in SECRETS} == SECRETS


def test_the_scheduler_makes_itself_not_dumpable_first_either_way_in():
    # python -m src.server.scheduler, its unit's: before the service is imported (a stand-in here, to see when).
    out = _python("import runpy, sys, types\n"
                  "from src.shared import secrets_env\n"
                  "order = []\n"
                  "secrets_env.not_dumpable = lambda: order.append('not_dumpable') or True\n"
                  "service = types.ModuleType('src.server.scheduler.service')\n"
                  "service.entry = lambda: order.append('service')\n"
                  "sys.modules['src.server.scheduler.service'] = service\n"
                  "runpy.run_module('src.server.scheduler', run_name='__main__')\n"
                  "print(order)")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == "['not_dumpable', 'service']"
    # entry(), the lumiverb-scheduler script's way in too: before main (which ends it, os._exit).
    out = _python("from src.shared import secrets_env\n"
                  "order = []\n"
                  "secrets_env.not_dumpable = lambda: order.append('not_dumpable') or True\n"
                  "from src.server.scheduler import service\n"
                  "service.main = lambda: print(order + ['main'], flush=True) or 0\n"
                  "service.entry()")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == "['not_dumpable', 'main']"


def test_the_plug_in_runner_keeps_only_its_secrets_then_makes_itself_not_dumpable(monkeypatch):
    from src.server import plugin_runner

    order = []

    def stop():
        order.append("not_dumpable")
        raise _StartedError

    monkeypatch.setattr(secrets_env, "keep_only", lambda keep: order.append(("keep_only", tuple(keep))))
    monkeypatch.setattr(secrets_env, "not_dumpable", stop)
    with pytest.raises(_StartedError):
        plugin_runner.main()
    assert order == [("keep_only", ("CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE",
                                    "LUMIVERB_SECRETS_KEY")), "not_dumpable"]
    assert order[0][1] == secrets_env.SCHEDULER_SECRETS, "a Mac's run hands it the same (server_children)"


def test_the_faces_process_makes_itself_not_dumpable_as_it_starts(monkeypatch):
    # The API's search by picture starts it, with the API's environment.
    from src.producers.faces import detect

    order = []
    monkeypatch.setattr(secrets_env, "not_dumpable", lambda: order.append("not_dumpable") or True)
    monkeypatch.setattr(os, "dup2", lambda *a: order.append("stdout"))
    detect.start_child()
    assert order == ["not_dumpable", "stdout"]
