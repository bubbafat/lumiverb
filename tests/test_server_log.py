"""A Mac's service log (src/server/lifecycle/log.py, lumiverb-server run):
each child's lines come through run, said by whom and without colour codes,
into launchd's file, which is rotated so it can't grow without end (10 MB
in an evening, a line per web request and Quickwit's chatter); the API's
own messages reach it too."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from src.server.lifecycle import log
from src.server.lifecycle.supervise import Child, Supervisor

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]


def test_a_childs_lines_are_said_by_whom_without_colour(caplog):
    caplog.set_level(logging.INFO, logger=log.RELAYED)
    out = io.BytesIO(b"\x1b[2m2026-10-12T00:53:49Z\x1b[0m \x1b[32m INFO\x1b[0m actor-exit\n\nplain line\r\n")
    log.relay("quickwit", out)
    assert [r.getMessage() for r in caplog.records if r.name == log.RELAYED] == [
        "quickwit | 2026-10-12T00:53:49Z  INFO actor-exit", "quickwit | plain line"]
    assert out.closed


def test_the_operating_systems_noise_is_dropped(caplog):
    # macOS, in each ffmpeg the scheduler starts once Metal is loaded: one a frame grabbed.
    caplog.set_level(logging.INFO, logger=log.RELAYED)
    log.relay("scheduler", io.BytesIO(
        b"Python(30032) MallocStackLogging: can't turn off malloc stack logging because it was not enabled.\n"
        b"2026-10-15 20:31:07,353 INFO scene-enrich: a.MOV\n"
        b"ffmpeg(41456) MallocStackLogging: can't turn off malloc stack logging because it was not enabled.\n"
        b"MallocStackLogging: can't turn off malloc stack logging because it was not enabled. (not this)\n"))
    assert [r.getMessage() for r in caplog.records if r.name == log.RELAYED] == [
        "scheduler | 2026-10-15 20:31:07,353 INFO scene-enrich: a.MOV",
        "scheduler | MallocStackLogging: can't turn off malloc stack logging because it was not enabled. (not this)"]


def test_launchds_file_is_the_one_kept_and_rotated(tmp_path):
    target = tmp_path / "lumiverb.log"
    with open(target, "a") as f:
        assert log.file_of(f.fileno()) == target
    r, w = os.pipe()
    try:
        assert log.file_of(w) is None, "a pipe or a terminal: no file to rotate"
    finally:
        os.close(r)
        os.close(w)


def _run_child(code: str, stderr_path: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    with open(stderr_path, "ab") as err:
        return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=REPO, stderr=err,
                              stdout=subprocess.PIPE, text=True, env={**os.environ, **(env or {})}, check=True,
                              timeout=120)


def test_the_log_rotates_and_what_the_process_prints_follows_it(tmp_path):
    # As launchd starts it: stderr is the log file. Rotated at a few KB here.
    path = tmp_path / "lumiverb.log"
    _run_child("""
        import logging, sys
        from src.server.lifecycle import log
        log.LOG_BYTES, log.LOG_KEPT = 2000, 2
        log.configure()
        for i in range(1000):  # ~16 KB: rotated several times over, so the oldest go
            logging.getLogger(log.RELAYED).info("api | line %d", i)
        logging.getLogger("lumiverb-server").info("api started")
        print("a stray traceback line", file=sys.stderr, flush=True)
    """, path)
    kept = sorted(p.name for p in tmp_path.iterdir())
    assert kept == ["lumiverb.log", "lumiverb.log.1", "lumiverb.log.2"], "bounded: the oldest went"
    assert all(p.stat().st_size <= 2200 for p in tmp_path.iterdir())
    current = path.read_text()
    assert "api | line 999" in current and "lumiverb-server: api started" in current
    assert "a stray traceback line" in current, "stderr follows the current file"


def test_configured_once_per_process(tmp_path):
    out = _run_child("""
        import logging
        from src.server.lifecycle import log
        first, second = log.configure(), log.configure()
        print(first is second, len(logging.getLogger().handlers))
    """, tmp_path / "x.log")
    assert out.stdout.split() == ["True", "1"]


def test_alembics_plugin_lines_are_left_out_its_migrations_kept(tmp_path):
    path = tmp_path / "lumiverb.log"
    _run_child("""
        import logging
        from src.server.lifecycle import log
        log.configure()
        logging.getLogger("alembic.runtime.plugins").info("setup plugin alembic.autogenerate.schemas")
        logging.getLogger("alembic.runtime.migration").info("Running upgrade a -> b")
    """, path)
    assert path.read_text().splitlines() == ["lumiverb-server: Running upgrade a -> b"]


def test_the_supervisor_relays_each_childs_output(caplog, tmp_path):
    caplog.set_level(logging.INFO, logger=log.RELAYED)
    child = Child("api", [sys.executable, "-c", "import sys; print('\\x1b[32mhello\\x1b[0m'); "
                                                "print('oops', file=sys.stderr)"],
                  dict(os.environ), tmp_path, restart_after=60, stop_within=5)
    supervisor = Supervisor([child], relay=log.relay)
    supervisor._start(child)
    child.process.wait(30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        said = sorted(r.getMessage() for r in caplog.records if r.name == log.RELAYED)
        if len(said) == 2:
            break
        time.sleep(0.05)
    assert said == ["api | hello", "api | oops"]


def test_the_apis_own_messages_come_through_at_the_installs_level(tmp_path):
    err = tmp_path / "api.log"
    _run_child("""
        import logging
        from src.shared.logging_config import configure_api_logging
        configure_api_logging()
        logging.getLogger("src.server.backups").info("Backed up 2 database(s)")
        logging.getLogger("httpx").info("a library's request line")
    """, err, env={"LOG_LEVEL": "INFO"})
    text = err.read_text()
    assert "INFO src.server.backups: Backed up 2 database(s)" in text
    assert "a library's request line" not in text, "libraries' info lines stay out"


def test_a_process_with_its_own_logging_keeps_it_when_it_imports_the_app(tmp_path):
    # The scheduler set INFO; the API's setup, imported after, must not lower
    # its lines to WARNING (LOG_LEVEL unset: the API's default).
    err = tmp_path / "scheduler.log"
    env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
    with open(err, "ab") as out:
        subprocess.run([sys.executable, "-c", textwrap.dedent("""
            import logging
            from src.server.scheduler.service import configure_logging
            from src.shared.logging_config import configure_api_logging
            configure_logging()
            configure_api_logging()
            logging.getLogger("src.server.scheduler.service").info("scheduler: started")
        """)], cwd=REPO, stderr=out, stdout=subprocess.PIPE, text=True, env=env, check=True, timeout=120)
    assert "INFO src.server.scheduler.service: scheduler: started" in err.read_text()
    assert err.read_text().count("scheduler: started") == 1, "one handler: said once"
