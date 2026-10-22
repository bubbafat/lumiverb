"""deploy-api.sh's `app` role, for a test: made by the script's own lines (its
step 4, from where it names psql to where it makes the control database) on
the test's PostgreSQL, as the install makes it on Linux.

The script runs them as root on the machine itself: `su - postgres` and
`sudo -u postgres` become the test's superuser over TCP (PGHOST, PGUSER and
PGPASSWORD in their environment), and python3 is this one, which has
psycopg2 as the install's python3-psycopg2 gives it. Every command line su
and sudo were given is returned: what any local user could have seen.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import make_url

DEPLOY_API = Path(__file__).resolve().parents[1] / "scripts" / "deploy-api.sh"

# su runs psql, which isn't here: the lines it's given are kept, and it does nothing.
STUBS = """\
su() { printf 'su %s\\n' "$*" >> "$ARGV_LOG"; }
sudo() { printf 'sudo %s\\n' "$*" >> "$ARGV_LOG"; shift 2; "$@"; }
"""


def role_step() -> str:
    text = DEPLOY_API.read_text()
    start = text.index('PSQL="psql -p ${PG_PORT}"\n')
    return text[start:text.index('su - postgres -c "${PSQL} -tc \\"SELECT 1 FROM pg_database', start)]


def make_app_role(superuser_url: str, role: str, password: str, tmp: Path) -> list[str]:
    """The role made (or put right) with password by deploy-api.sh's lines, as
    superuser_url's user; the command lines su and sudo were given."""
    server = make_url(superuser_url)
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "python3").chmod(0o755)
    argv_log = tmp / "argv.log"
    argv_log.write_text("")
    out = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", STUBS + role_step()], capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "ARGV_LOG": str(argv_log),
             "PGHOST": server.host or "127.0.0.1", "PGUSER": server.username or "", "PGPASSWORD": server.password or "",
             "PG_PORT": str(server.port or 5432), "PG_USER": role, "PG_PASS": password},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    return argv_log.read_text().splitlines()
