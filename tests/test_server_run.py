"""lumiverb-server run (src/server/lifecycle/cli.py): a Mac's service. The
children it runs match the brain's systemd units; migrations wait for
Postgres; and end to end, on real Postgres, it migrates, serves the API,
runs the scheduler, and stops both cleanly on SIGTERM."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from testcontainers.postgres import PostgresContainer

from src.server.lifecycle import cli
from tests.conftest import PG_IMAGE, _ensure_psycopg2


@pytest.mark.fast
def test_the_children_are_the_brains_units(tmp_path):
    env = {"DATA_DIR": str(tmp_path), "API_PORT": "8100", "API_LISTEN_HOST": "0.0.0.0", "ADMIN_KEY": "k"}
    api, scheduler = cli.server_children(env, python="/py", cwd=tmp_path)

    assert api.argv == ["/py", "-m", "uvicorn", "src.server.api.main:app",
                        "--host", "0.0.0.0", "--port", "8100", "--workers", "2"]
    assert api.env["TMPDIR"] == str(tmp_path / "tmp") and api.empty_first == tmp_path / "tmp"
    assert (api.restart_after, api.stop_within, api.term_main_only) == (5, 90, False)

    assert scheduler.argv == ["/py", "-m", "src.server.scheduler"]
    assert scheduler.env["TMPDIR"] == str(tmp_path / "worker-tmp") and scheduler.empty_first == tmp_path / "worker-tmp"
    assert scheduler.env["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert (scheduler.restart_after, scheduler.stop_within, scheduler.term_main_only) == (30, 40, True)
    assert api.env["PYTHONUNBUFFERED"] == scheduler.env["PYTHONUNBUFFERED"] == "1"
    assert scheduler.env["ADMIN_KEY"] == "k"  # the install's variables reach both


@pytest.mark.fast
def test_api_defaults_and_workers_setting(tmp_path):
    api, _ = cli.server_children({"DATA_DIR": str(tmp_path)}, python="/py", cwd=tmp_path)
    assert api.argv[-6:] == ["--host", "127.0.0.1", "--port", "8000", "--workers", "2"]
    api, _ = cli.server_children({"DATA_DIR": str(tmp_path), "LUMIVERB_API_WORKERS": "4"}, python="/py", cwd=tmp_path)
    assert api.argv[-1] == "4"


@pytest.mark.fast
def test_migrations_wait_for_postgres():
    calls: list[str] = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("connection refused")
        return 0

    assert cli.migrate_when_ready("db", threading.Event(), migrate_fn=flaky, first_wait=0.01) is True
    assert len(calls) == 3


@pytest.mark.fast
def test_a_stop_while_waiting_for_postgres_ends_it():
    stop = threading.Event()

    def never(url):
        stop.set()
        raise OSError("connection refused")

    assert cli.migrate_when_ready("db", stop, migrate_fn=never, first_wait=0.01) is False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


@pytest.mark.slow
def test_run_migrates_serves_and_stops_cleanly(tmp_path):
    lumiverb_server = Path(sys.executable).parent / "lumiverb-server"  # the console script a formula installs
    assert lumiverb_server.exists()
    with PostgresContainer(PG_IMAGE) as pg:
        # A control database that isn't there yet: run makes it (no createdb on a Mac).
        server_url = make_url(_ensure_psycopg2(pg.get_connection_url()))
        control_url = server_url.set(database=f"made_by_run_{server_url.database}").render_as_string(
            hide_password=False)
        engine = create_engine(control_url)
        port = _free_port()
        env_file = tmp_path / "env"
        env_file.write_text("\n".join([
            f"CONTROL_PLANE_DATABASE_URL={control_url}",
            f"TENANT_DATABASE_URL_TEMPLATE={control_url.rsplit('/', 1)[0]}/{{tenant_id}}",
            f"DATA_DIR={tmp_path / 'data'}",
            f"API_PORT={port}",
            "API_LISTEN_HOST=127.0.0.1",
            "LUMIVERB_API_WORKERS=1",
            "ADMIN_KEY=test-admin-run",
            "JWT_SECRET=test-jwt-run",
            "QUICKWIT_ENABLED=false",  # whatever a developer's .env.local says
        ]))
        out = tmp_path / "service.log"
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("CONTROL_PLANE", "TENANT_DATABASE", "LUMIVERB_", "API_", "DATA_DIR"))}
        with out.open("w") as log:
            service = subprocess.Popen([str(lumiverb_server), "--env", str(env_file), "run"],
                                       stdout=log, stderr=subprocess.STDOUT, env=clean_env)
        try:
            deadline = time.monotonic() + 120
            while not _healthy(port):
                assert service.poll() is None, out.read_text()
                assert time.monotonic() < deadline, out.read_text()
                time.sleep(0.5)
            with engine.connect() as conn:
                assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            # In the log while it runs, not held in a buffer until it stops.
            running = out.read_text()
            assert "lumiverb-server: Created the control database" in running, running
            assert "lumiverb-server: Control plane" in running, running
        finally:
            service.send_signal(signal.SIGTERM)
            try:
                code = service.wait(timeout=120)
            except subprocess.TimeoutExpired:
                service.kill()
                raise
            engine.dispose()

    said = out.read_text()
    assert code == 0, said
    assert "api started" in said and "scheduler started" in said, said
    assert said.index("scheduler stopped") < said.index("api stopped"), said
