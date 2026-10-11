"""A Mac install's own PostgreSQL: a cluster of its own in DATA_DIR/postgres,
on this machine only, its role and password the control database URL's.
`lumiverb-server run` makes it on first start (initdb), then runs it as a
child, so nothing is shared with another PostgreSQL on the Mac (Homebrew's
on 5432, a dev checkout's)."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import make_url


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


def postgres_argv(pg_bin: Path, data_dir: Path, url: str) -> list[str]:
    """The postmaster, on the control database URL's port, this machine only."""
    port = make_url(url).port or 5432
    return [str(pg_bin / "postgres"), "-D", str(cluster_dir(data_dir)), "-p", str(port),
            "-c", "listen_addresses=127.0.0.1"]
