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

from src.server.lifecycle.postgres import (
    PostgresError,
    make_cluster_if_missing,
    postgres_argv,
    postgres_env,
)


def postgres_bin() -> Path | None:
    """A PostgreSQL bin folder on this machine with initdb, or None."""
    candidates = [os.environ.get("LUMIVERB_TEST_PG_BIN"), "/opt/homebrew/opt/postgresql@18/bin",
                  "/usr/local/opt/postgresql@18/bin", "/usr/lib/postgresql/18/bin"]
    if shutil.which("pg_config"):
        candidates.append(subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True).stdout.strip())
    return next((Path(c) for c in candidates if c and (Path(c) / "initdb").exists()), None)


NO_POSTGRES = pytest.mark.skipif(postgres_bin() is None, reason="no PostgreSQL (initdb) on this machine")


def _has_vector(pg_bin: Path | None) -> bool:
    """Whether that PostgreSQL has pgvector, which an account's database needs."""
    if pg_bin is None or not (pg_bin / "pg_config").exists():
        return False
    share = subprocess.run([str(pg_bin / "pg_config"), "--sharedir"], capture_output=True, text=True).stdout.strip()
    return bool(share) and (Path(share) / "extension" / "vector.control").exists()


NO_VECTOR = pytest.mark.skipif(not _has_vector(postgres_bin()), reason="no pgvector for this machine's PostgreSQL")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.fast
def test_it_listens_on_the_url_s_port_on_this_machine_only(tmp_path):
    assert postgres_argv(Path("/pg"), tmp_path, "postgresql+psycopg2://lumiverb:pw@127.0.0.1:6468/control_plane") == [
        "/pg/postgres", "-D", str(tmp_path / "postgres"), "-p", "6468", "-c", "listen_addresses=127.0.0.1",
        "-c", "unix_socket_directories="]


@pytest.mark.fast
def test_it_has_a_locale_of_its_own():
    # Without one, macOS's postmaster refuses to start under launchd.
    assert postgres_env({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin", "LC_ALL": "C"}


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

    # As run starts it: launchd's environment, no locale of its own.
    bare = postgres_env({"PATH": "/usr/bin:/bin"})
    server = subprocess.Popen(postgres_argv(pg_bin, tmp_path, url), env=bare, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
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


@pytest.mark.slow
@NO_POSTGRES
@NO_VECTOR
def test_migrate_to_starts_a_stopped_install_s_own_postgresql_for_the_while(tmp_path, monkeypatch, capsys):
    # A Mac's rollback (ADR-024 phase 1): with the service stopped, its PostgreSQL is too. migrate --to starts it,
    # takes the accounts down (or up), and stops it again, as backup and restore do.
    from src.server.lifecycle import cli
    from src.server.lifecycle.migrate import create_database_if_missing, migrate
    from src.server.lifecycle.postgres import answers, up_while
    from tests.test_server_migrate import _add_tenant, _before, _head, _version, _with_vector

    pg_bin, port = postgres_bin(), free_port()
    data = tmp_path / "data"
    server = f"postgresql+psycopg2://lumiverb:pw@127.0.0.1:{port}"
    control_url, tenant_url = f"{server}/control_plane", f"{server}/ten_one"
    make_cluster_if_missing(pg_bin, data, control_url)
    with up_while(pg_bin, data, control_url, dict(os.environ)):
        for url in (control_url, tenant_url):
            create_database_if_missing(url)
            _with_vector(url)
        migrate(control_url, say=lambda _: None)
        _add_tenant(control_url, "ten_one", tenant_url)
        migrate(control_url, say=lambda _: None)
    assert not answers(control_url), "stopped, as a stopped service's is"
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATA_DIR")  # the test process's own (tests/conftest.py) would win over the file's
    env_file = tmp_path / "env"
    env_file.write_text(f"CONTROL_PLANE_DATABASE_URL={control_url}\nDATA_DIR={data}\nPG_BIN={pg_bin}\n")
    back, head = _before("alembic-tenant.ini"), _head("alembic-tenant.ini")

    assert cli.main(["--env", str(env_file), "migrate", "--to", back]) == 0, capsys.readouterr().err
    assert "✓ 1 account(s) at " + back in capsys.readouterr().out
    assert not answers(control_url), "stopped again"
    with up_while(pg_bin, data, control_url, dict(os.environ)):
        assert _version(tenant_url) == back
    assert cli.main(["--env", str(env_file), "migrate", "--to", head]) == 0, capsys.readouterr().err
    assert not answers(control_url)
    with up_while(pg_bin, data, control_url, dict(os.environ)):
        assert _version(tenant_url) == head
