"""Database migrations for an install: the control plane, then every
account's database. scripts/migrate.sh's steps (a Linux install's update runs
that), as a function a service can run before it starts, on any platform.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError

# The checkout or install: where alembic-*.ini and migrations/ are.
APP_DIR = Path(__file__).resolve().parents[3]

# A traceback's exception line ("sqlalchemy.exc.OperationalError: ..."); what
# follows it can be a hint or a link ("(Background on this error at: ...)").
_EXCEPTION_LINE = re.compile(r"^\w[\w.]*(Error|Exception):")


class MigrationError(RuntimeError):
    pass


def _upgrade(ini: str, variable: str, url: str, cwd: Path, *, to: tuple[str, str] = ("upgrade", "head")
             ) -> list[str] | None:
    """Alembic up to head (or, to, another way: ("downgrade", a revision)); None when it worked, else
    everything it said (a traceback's last line can lose the cause: migrate.sh prints the whole log)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, *to],
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


def create_database_if_missing(url: str) -> bool:
    """Creates the URL's database when its server doesn't have it (connecting
    to the server's own `postgres` database); True if it did. For a Mac's
    service, so there's no createdb to type; Linux's installer makes its
    own. Raises MigrationError when the server can't be reached."""
    target = make_url(url)
    name = target.database
    if not name:
        raise MigrationError(f"No database named in {target.render_as_string(hide_password=True)}")
    engine = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}).scalar():
                return False
            conn.execute(text(f'CREATE DATABASE "{name.replace(chr(34), chr(34) * 2)}"'))
            return True
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise MigrationError(f"Can't create the database {name}: {first}") from exc
    finally:
        engine.dispose()


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


def _behind(url: str, scripts: Path) -> bool:
    """Whether the database's migrations are behind this version's."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    # Alembic says what it sets up at INFO; the service's log is for what happened.
    logging.getLogger("alembic").setLevel(logging.WARNING)
    heads = set(ScriptDirectory(str(scripts)).get_heads())
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(MigrationContext.configure(conn).get_current_heads()) != heads
    finally:
        engine.dispose()


def pending(control_url: str, *, cwd: Path = APP_DIR) -> bool:
    """Whether migrating would change anything: the control plane's or an
    account's database is behind. False for a new install with nothing in it
    yet (nothing to back up). Raises MigrationError when it can't tell."""
    try:
        if _behind(control_url, cwd / "migrations/control"):
            engine = create_engine(control_url)
            try:
                with engine.connect() as conn:
                    has_routing = conn.execute(text("SELECT to_regclass('tenant_db_routing') IS NOT NULL")).scalar()
            finally:
                engine.dispose()
            return bool(has_routing)
        return any(_behind(url, cwd / "migrations/tenant") for _, url in tenant_databases(control_url))
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise MigrationError(f"Can't tell whether the databases need migrating: {first}") from exc


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


def _way_to(url: str, scripts: Path, revision: str) -> str | None:
    """How a database gets to this revision: "downgrade" (it's below where the database is), "upgrade", or None
    (it's there). MigrationError for a revision these migrations don't have."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from alembic.util import CommandError

    logging.getLogger("alembic").setLevel(logging.WARNING)
    script = ScriptDirectory(str(scripts))
    try:
        script.get_revision(revision)
    except CommandError as exc:
        raise MigrationError(f"No revision {revision} in {scripts.name}'s migrations: {exc}") from None
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            current = tuple(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()
    if current == (revision,):
        return None
    below = {r.revision for r in script.iterate_revisions(current, "base")} if current else set()
    return "downgrade" if revision in below else "upgrade"


def migrate_to(control_url: str, *, tenant: str | None, control: str | None = None, cwd: Path = APP_DIR,
               say: Callable[[str], None] = print) -> int:
    """Every account's database (tenant) and the control plane's (control) taken to these revisions, down or up:
    a rollback, run by the newer code, which knows them (older code can't migrate down from revisions it
    doesn't have). Each account tried even when another fails. Returns how many accounts it moved; raises
    MigrationError saying which failed and why."""
    if control is not None:
        way = _way_to(control_url, cwd / "migrations/control", control)
        said = _upgrade("alembic-control.ini", "ALEMBIC_CONTROL_URL", control_url, cwd, to=(way, control)) \
            if way else None
        if said:
            say(f"Control plane ✗ {_cause(said)}")
            _say_all(said, say)
            raise MigrationError(f"The control plane's {way} to {control} failed: {_cause(said)}")
        say(f"Control plane ✓ {'at ' + control if way is None else way + 'd to ' + control}")
    if tenant is None:
        return 0
    tenants = tenant_databases(control_url)
    say(f"Accounts' databases ({len(tenants)}), to {tenant}")
    failed: list[str] = []
    for number, (tenant_id, url) in enumerate(tenants, 1):
        try:
            way = _way_to(url, cwd / "migrations/tenant", tenant)
        except (MigrationError, SQLAlchemyError) as exc:
            failed.append(f"{tenant_id}: {exc}")
            say(f"  [{number}/{len(tenants)}] {tenant_id} ✗ {exc}")
            continue
        said = _upgrade("alembic-tenant.ini", "ALEMBIC_TENANT_URL", url, cwd, to=(way, tenant)) if way else None
        say(f"  [{number}/{len(tenants)}] {tenant_id} "
            f"{'✗ ' + _cause(said) if said else '✓ ' + (way + 'd' if way else 'there already')}")
        if said:
            _say_all(said, say)
            failed.append(f"{tenant_id}: {_cause(said)}")
    if failed:
        raise MigrationError(f"{len(failed)} of {len(tenants)} accounts didn't get to {tenant}: " + "; ".join(failed))
    return len(tenants)
