"""Database migrations for an install: the control plane, then every
account's database. scripts/migrate.sh's steps (the brain's update runs
that), as a function a service can run before it starts, on any platform.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# The checkout or install: where alembic-*.ini and migrations/ are.
APP_DIR = Path(__file__).resolve().parents[3]

# A traceback's exception line ("sqlalchemy.exc.OperationalError: ..."); what
# follows it can be a hint or a link ("(Background on this error at: ...)").
_EXCEPTION_LINE = re.compile(r"^\w[\w.]*(Error|Exception):")


class MigrationError(RuntimeError):
    pass


def _upgrade(ini: str, variable: str, url: str, cwd: Path) -> list[str] | None:
    """Alembic up to head; None when it worked, else everything it said (a
    traceback's last line can lose the cause: migrate.sh prints the whole log)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, "upgrade", "head"],
        cwd=cwd, env={**os.environ, variable: url}, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return None
    said = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return said.splitlines() or [f"alembic exited {result.returncode}"]


def _cause(said: list[str]) -> str:
    """The one line that says why: the last exception line, else the last line."""
    return next((line for line in reversed(said) if _EXCEPTION_LINE.match(line)), said[-1])


def _say_all(said: list[str], say: Callable[[str], None]) -> None:
    for line in said:
        say(f"      {line}")


def tenant_databases(control_url: str) -> list[tuple[str, str]]:
    """Each account's id and database URL, from the control plane. Raises
    MigrationError when the control plane can't be read."""
    engine = create_engine(control_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, connection_string FROM tenant_db_routing ORDER BY tenant_id"
            )).all()
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise MigrationError(f"Can't read the accounts from the control plane: {first}") from exc
    finally:
        engine.dispose()
    return [(row[0], row[1]) for row in rows]


def migrate(control_url: str, *, cwd: Path = APP_DIR, say: Callable[[str], None] = print) -> int:
    """Migrates the control plane, then every account's database (each one
    tried even when another fails). Returns how many accounts it migrated;
    raises MigrationError saying which failed and why."""
    say("Control plane")
    said = _upgrade("alembic-control.ini", "ALEMBIC_CONTROL_URL", control_url, cwd)
    if said:
        say(f"  ✗ {_cause(said)}")
        _say_all(said, say)
        raise MigrationError(f"The control plane's migration failed: {_cause(said)}")
    say("  ✓ up to date")

    tenants = tenant_databases(control_url)
    say(f"Accounts' databases ({len(tenants)})")
    failed: list[str] = []
    for number, (tenant_id, url) in enumerate(tenants, 1):
        said = _upgrade("alembic-tenant.ini", "ALEMBIC_TENANT_URL", url, cwd)
        say(f"  [{number}/{len(tenants)}] {tenant_id} {'✗ ' + _cause(said) if said else '✓'}")
        if said:
            _say_all(said, say)
            failed.append(f"{tenant_id}: {_cause(said)}")
    if failed:
        raise MigrationError(f"{len(failed)} of {len(tenants)} accounts' migrations failed: " + "; ".join(failed))
    return len(tenants)
