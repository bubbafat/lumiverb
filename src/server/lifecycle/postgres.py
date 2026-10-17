"""A Mac install's own PostgreSQL: a cluster of its own in DATA_DIR/postgres,
on this machine only, its superuser and password DATABASE_SUPERUSER_URL's
(before run moved the env file over, the control database URL's; the
databases are an ordinary role's: roles.py).
`lumiverb-server run` makes it on first start (initdb), then runs it as a
child, so nothing is shared with another PostgreSQL on the Mac (Homebrew's
on 5432, a dev checkout's)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError


class PostgresError(RuntimeError):
    pass


def cluster_dir(data_dir: Path) -> Path:
    return data_dir / "postgres"


def make_cluster_if_missing(pg_bin: Path, data_dir: Path, url: str) -> bool:
    """initdb the install's cluster unless it's there; True if it made it.
    Raises PostgresError saying why initdb failed."""
    cluster = cluster_dir(data_dir)
    if (cluster / "PG_VERSION").exists():
        return False
    target = make_url(url)
    if not target.username or not target.password:
        raise PostgresError("CONTROL_PLANE_DATABASE_URL names no user and password for Lumiverb's PostgreSQL")
    cluster.parent.mkdir(parents=True, exist_ok=True)
    fd, pwfile = tempfile.mkstemp(prefix="lumiverb-pg-")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(target.password)
        result = subprocess.run(
            [str(pg_bin / "initdb"), "-D", str(cluster), "-U", target.username, f"--pwfile={pwfile}",
             "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(pwfile)
    if result.returncode != 0:
        said = (result.stderr or result.stdout).strip().splitlines() or [f"initdb exited {result.returncode}"]
        raise PostgresError(f"Couldn't make Lumiverb's database in {cluster}: {said[-1]}")
    return True


def postgres_env(env: dict[str, str]) -> dict[str, str]:
    """Its environment: a locale set, as Homebrew's own PostgreSQL service
    sets it. Without one (launchd's), macOS's locale lookup starts threads in
    the postmaster, which then refuses to start ("postmaster became
    multithreaded during startup")."""
    return {**env, "LC_ALL": "C"}


def postgres_argv(pg_bin: Path, data_dir: Path, url: str) -> list[str]:
    """The postmaster, on the control database URL's port, this machine only,
    and no Unix socket: Lumiverb connects over TCP, and a build's default
    socket folder may not be ours to write (Debian's /var/run/postgresql)."""
    port = make_url(url).port or 5432
    return [str(pg_bin / "postgres"), "-D", str(cluster_dir(data_dir)), "-p", str(port),
            "-c", "listen_addresses=127.0.0.1", "-c", "unix_socket_directories="]


def answers(url: str) -> bool:
    """Whether the URL's server takes connections (its own `postgres` database)."""
    engine = create_engine(make_url(url).set(database="postgres"))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
    finally:
        engine.dispose()


START_WAIT_SEC = 30.0


@contextlib.contextmanager
def up_while(pg_bin: Path | None, data_dir: Path, url: str, env: dict[str, str]) -> Iterator[None]:
    """The install's PostgreSQL taking connections meanwhile: already up, or
    (a Mac's own, with the service stopped) started for the while and
    stopped after. Raises PostgresError when it can't be reached."""
    if answers(url):
        yield
        return
    if pg_bin is None or not (cluster_dir(data_dir) / "PG_VERSION").exists():
        raise PostgresError("Can't reach the database. Is PostgreSQL running?")
    process = subprocess.Popen(postgres_argv(pg_bin, data_dir, url), env=postgres_env(env),
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + START_WAIT_SEC
        while not answers(url):
            if process.poll() is not None:
                said = (process.stderr.read() if process.stderr else "").strip().splitlines()
                raise PostgresError(f"Lumiverb's PostgreSQL didn't start: {said[-1] if said else process.returncode}")
            if time.monotonic() > deadline:
                raise PostgresError("Lumiverb's PostgreSQL didn't start within 30 seconds")
            time.sleep(0.5)
        yield
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)  # a fast shutdown, as run stops it
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
