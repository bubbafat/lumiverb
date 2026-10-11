"""A Mac install's own PostgreSQL (src/server/lifecycle/postgres.py): its
cluster made once with initdb, as the control database URL's role and
password, on this machine only. With a real PostgreSQL where one is
installed (Homebrew's postgresql@18, Debian's postgresql-18)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.server.lifecycle.postgres import PostgresError, make_cluster_if_missing, postgres_argv


def postgres_bin() -> Path | None:
    """A PostgreSQL bin folder on this machine with initdb, or None."""
    candidates = [os.environ.get("LUMIVERB_TEST_PG_BIN"), "/opt/homebrew/opt/postgresql@18/bin",
                  "/usr/local/opt/postgresql@18/bin", "/usr/lib/postgresql/18/bin"]
    if shutil.which("pg_config"):
        candidates.append(subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True).stdout.strip())
    return next((Path(c) for c in candidates if c and (Path(c) / "initdb").exists()), None)


NO_POSTGRES = pytest.mark.skipif(postgres_bin() is None, reason="no PostgreSQL (initdb) on this machine")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.fast
def test_it_listens_on_the_url_s_port_on_this_machine_only(tmp_path):
    assert postgres_argv(Path("/pg"), tmp_path, "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/control_plane") == [
        "/pg/postgres", "-D", str(tmp_path / "postgres"), "-p", "6468", "-c", "listen_addresses=127.0.0.1"]


@pytest.mark.fast
def test_a_url_without_a_password_is_refused_before_anything_is_made(tmp_path):
    with pytest.raises(PostgresError, match="names no user and password"):
        make_cluster_if_missing(Path("/pg"), tmp_path, "postgresql+psycopg2://lumiverb@127.0.0.1:6468/cp")
    assert not (tmp_path / "postgres").exists()


@pytest.mark.slow
@NO_POSTGRES
def test_made_once_then_its_password_and_nothing_else_lets_you_in(tmp_path):
    pg_bin, port = postgres_bin(), free_port()
    url = f"postgresql+psycopg2://lumiverb:right-password@127.0.0.1:{port}/postgres"
    assert make_cluster_if_missing(pg_bin, tmp_path, url) is True
    assert make_cluster_if_missing(pg_bin, tmp_path, url) is False, "made once"

    server = subprocess.Popen(postgres_argv(pg_bin, tmp_path, url), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                engine = create_engine(url)
                with engine.connect() as conn:
                    assert conn.execute(text("SELECT current_user")).scalar() == "lumiverb"
                engine.dispose()
                break
            except OperationalError:
                assert server.poll() is None, server.stderr.read().decode()
                assert time.monotonic() < deadline
                time.sleep(0.2)
        wrong = create_engine(url.replace("right-password", "wrong-password"))
        with pytest.raises(OperationalError, match="password authentication failed"):
            wrong.connect()
        wrong.dispose()
    finally:
        server.terminate()
        server.wait(30)


@pytest.mark.slow
@NO_POSTGRES
def test_initdb_failing_is_one_line(tmp_path):
    (tmp_path / "postgres").mkdir()
    (tmp_path / "postgres" / "not-empty").write_text("someone's")  # initdb wants an empty folder
    with pytest.raises(PostgresError, match=r"^Couldn't make Lumiverb's database in "):
        make_cluster_if_missing(postgres_bin(), tmp_path, "postgresql+psycopg2://lumiverb:pw@127.0.0.1:1/x")
