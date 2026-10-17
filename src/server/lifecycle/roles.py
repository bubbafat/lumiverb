"""A Mac install's database roles, as Linux's (Robert, 2026-10-15: "Match
Linux").

Lumiverb's own PostgreSQL is made by initdb, whose bootstrap user is a
superuser: until this, the API and the scheduler connected as it. Linux's
install connects as an ordinary role that may create databases and owns
everything (deploy-api.sh). Now a Mac's does too:

- `lumiverb_app` (APP_ROLE): LOGIN and CREATEDB, nothing more. It owns the
  control database, every account's and everything in them, and does
  everything: requests, the scheduler, migrations, a new account's database,
  backups and restores (as Linux's role does them: backups.py restores with
  --no-owner, and `vector` comes with template1).
- The superuser stays only for what needs one: making that role and keeping
  its password the env file's, `vector` in template1 and in each database,
  and handing over what it owned (ensure_app_role). Only `lumiverb-server run`
  has its URL (SUPERUSER_URL): never the API, the scheduler or anything they
  start.

An install from before this is moved over by run's first start: the env
file's database URLs become the role's, with a new password, the superuser's
kept as SUPERUSER_URL, the file backed up first and changed line by line,
nothing else in it touched (with_app_role). Then, once PostgreSQL answers,
ensure_app_role (every start: it does nothing when nothing needs it).
"""

from __future__ import annotations

import logging
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, make_url, text

logger = logging.getLogger(__name__)

APP_ROLE = "lumiverb_app"
SUPERUSER_URL = "DATABASE_SUPERUSER_URL"
URLS = ("CONTROL_PLANE_DATABASE_URL", "TENANT_DATABASE_URL_TEMPLATE")


class RoleError(RuntimeError):
    pass


# -- The env file -------------------------------------------------------------------------


def _as(url: str, user: str, password: str) -> str:
    """url with another user and password. Works on a template too: its
    {tenant_id} is only in the database part."""
    scheme, rest = url.split("://", 1)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://{user}:{password}@{host}"


def with_app_role(env_file: Path, env: dict[str, str], *, password: str | None = None,
                  now: datetime | None = None) -> dict[str, str]:
    """The env, as the app role: unchanged when it already is (it names
    SUPERUSER_URL). Otherwise the file is backed up beside itself
    (env.before-app-role-<time>), its two database URLs rewritten to the
    role's with a new password, and the superuser's URL added after them;
    the rest of the file as it was. Returns the env as it now is."""
    if env.get(SUPERUSER_URL) or not env.get("CONTROL_PLANE_DATABASE_URL"):
        return env
    superuser = env["CONTROL_PLANE_DATABASE_URL"]
    try:
        named = make_url(superuser)
    except Exception:  # noqa: BLE001 — not a URL: nothing to move over (initdb, then the API, say why)
        return env
    if not named.username or not named.password:
        return env  # no superuser to keep: initdb refuses it, saying so (postgres.py)
    if named.username == APP_ROLE:
        raise RoleError(f"{env_file} connects as {APP_ROLE} but names no {SUPERUSER_URL}")
    password = password or secrets.token_urlsafe(32)
    new = {name: _as(env[name], APP_ROLE, password) for name in URLS if env.get(name)}
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(env_file, env_file.with_name(f"{env_file.name}.before-app-role-{stamp}"))
    lines, added = [], False
    for line in env_file.read_text().splitlines():
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in new:
            lines.append(f"{name}={new[name]}")
            if name == "TENANT_DATABASE_URL_TEMPLATE" or "TENANT_DATABASE_URL_TEMPLATE" not in new:
                lines += ["# The superuser: lumiverb-server run's alone, to make the role above and hand",
                          "# it what it owns (src/server/lifecycle/roles.py). Nothing else gets it.",
                          f"{SUPERUSER_URL}={superuser}"]
                added = True
        else:
            lines.append(line)
    if not added:
        lines.append(f"{SUPERUSER_URL}={superuser}")
    part = env_file.with_name(env_file.name + ".part")
    part.write_text("\n".join(lines) + "\n")
    part.chmod(env_file.stat().st_mode & 0o777)
    part.replace(env_file)
    return {**env, **new, SUPERUSER_URL: superuser}


# -- The database ---------------------------------------------------------------------------

# What a database holds that its owner should: every object outside the
# system's schemas that isn't part of an extension (vector's are the
# superuser's, as on Linux). A table's indexes, and the sequences its columns
# own, go with it.
_OBJECTS = """
SELECT format('ALTER SCHEMA %I OWNER TO %I', n.nspname, :role)
FROM pg_namespace n
WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
  AND n.nspowner <> (SELECT oid FROM pg_roles WHERE rolname = :role)
  AND n.nspowner <> (SELECT oid FROM pg_roles WHERE rolname = 'pg_database_owner')
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_namespace'::regclass AND d.objid = n.oid AND d.deptype = 'e')
UNION ALL
SELECT format('ALTER %s %I.%I OWNER TO %I',
              CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'f' THEN 'FOREIGN TABLE'
                             WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'SEQUENCE' END,
              n.nspname, c.relname, :role)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'f', 'v', 'm', 'S')
  AND n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
  AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :role)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
  AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                                        AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
UNION ALL
SELECT format('ALTER TYPE %I.%I OWNER TO %I', n.nspname, t.typname, :role)
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.typtype IN ('e', 'd', 'r')  -- a multirange goes with its range
  AND n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
  AND t.typowner <> (SELECT oid FROM pg_roles WHERE rolname = :role)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
UNION ALL
SELECT format('ALTER ROUTINE %I.%I(%s) OWNER TO %I', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), :role)
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p')
  AND n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
  AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = :role)
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
"""


def _autocommit(url: str):
    return create_engine(url, isolation_level="AUTOCOMMIT")


def _role(superuser_url: str, role: str, password: str) -> bool:
    """The role made, or put right: LOGIN and CREATEDB only, the password
    given. True when it was made."""
    engine = _autocommit(make_url(superuser_url).set(database="postgres"))
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
            verb = "ALTER" if exists else "CREATE"
            statement = conn.execute(text(
                f"SELECT format('{verb} ROLE %I LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                " PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"), {"r": role, "p": password}).scalar()
            conn.execute(text(statement))
            return not exists
    finally:
        engine.dispose()


def _owned(superuser_url: str, database: str, role: str) -> int:
    """The database, and everything in it, the role's; `vector` there. How
    many things changed hands."""
    changed = 0
    admin = _autocommit(make_url(superuser_url).set(database="postgres"))
    try:
        with admin.connect() as conn:
            owner = conn.execute(text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d"),
                                 {"d": database}).scalar()
            if owner is None:
                return 0
            if owner != role:
                conn.execute(text(conn.execute(text("SELECT format('ALTER DATABASE %I OWNER TO %I', "
                                                    "CAST(:d AS text), CAST(:r AS text))"),
                                               {"d": database, "r": role}).scalar()))
                changed += 1
    finally:
        admin.dispose()
    engine = _autocommit(make_url(superuser_url).set(database=database))
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            for (statement,) in conn.execute(text(_OBJECTS), {"role": role}).all():
                conn.execute(text(statement))
                changed += 1
    finally:
        engine.dispose()
    return changed


def ensure_app_role(superuser_url: str, control_url: str) -> dict[str, int]:
    """As the superuser: the app role (control_url's user and password)
    made or put right; `vector` in template1, so every database made from it
    has it; the control database, every account's (their routing) and all
    in them the role's; the routing rows the role's credentials. What changed,
    counted (all 0 when nothing needed it)."""
    app = make_url(control_url)
    if not app.username or not app.password or not app.database:
        raise RoleError("The control database URL names no user, password or database")
    made = _role(superuser_url, app.username, app.password)
    engine = _autocommit(make_url(superuser_url).set(database="template1"))
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()

    handed = _owned(superuser_url, app.database, app.username)
    rerouted = 0
    if not _exists(superuser_url, app.database):  # a new install's: made by the role, then migrated
        return _said({"role_made": int(made), "handed_over": handed, "rerouted": 0}, app.username)
    control = create_engine(make_url(superuser_url).set(database=app.database))
    try:
        with control.begin() as conn:
            has_routing = conn.execute(text("SELECT to_regclass('public.tenant_db_routing')")).scalar()
            routes = conn.execute(text("SELECT tenant_id, connection_string FROM tenant_db_routing")).all() \
                if has_routing else []
            for tenant_id, url in routes:
                target = make_url(url)
                if target.database:
                    handed += _owned(superuser_url, target.database, app.username)
                if (target.username, target.password) != (app.username, app.password):
                    conn.execute(text("UPDATE tenant_db_routing SET connection_string = :u WHERE tenant_id = :t"),
                                 {"u": _as(url, app.username, app.password), "t": tenant_id})
                    rerouted += 1
    finally:
        control.dispose()
    return _said({"role_made": int(made), "handed_over": handed, "rerouted": rerouted}, app.username)


def _exists(superuser_url: str, database: str) -> bool:
    engine = create_engine(make_url(superuser_url).set(database="postgres"))
    try:
        with engine.connect() as conn:
            return bool(conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": database}).scalar())
    finally:
        engine.dispose()


def _said(done: dict[str, int], role: str) -> dict[str, int]:
    if any(done.values()):
        logger.info("Database: %s owns Lumiverb's databases (%s)", role,
                    ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in done.items()))
    return done
