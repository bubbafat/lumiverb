"""Lumiverb test suite.

Shared helpers for slow tests (testcontainers + control/tenant DB setup):
- _ensure_psycopg2: normalize Postgres URL for SQLAlchemy
- _run_control_migrations: bring a database to alembic-control.ini head
- _provision_tenant_db: bring a database to alembic-tenant.ini heads, with pgvector
- _AuthClient: TestClient wrapper that adds Authorization header to get/post

One Postgres for the whole run. Test modules start a `PostgresContainer(PG_IMAGE)`
of their own, often two; this file swaps that name for `_Database`, a fresh
empty database on a single server started once per run (once for every
worker under `-n`), with durability off and its data in memory. Each one is
dropped on exit, so modules stay as isolated from each other as they were.
The two helpers above clone an empty database from a template migrated once
per run instead of migrating it again; a test that runs alembic itself (the
migration tests) still migrates for real, as does anything marked
`migration`. LUMIVERB_TEST_PG_PER_MODULE=1 goes back to a container per
module and real migrations everywhere.

Parallel: `pytest -n auto` (pytest-xdist). Each module's tests stay together
on one worker (`--dist loadfile` unless you pick another `--dist`).
"""

import contextlib
import itertools
import os
import subprocess
import sys

# Use NullPool for all test engines so connections are never held idle.
# This prevents "server closed the connection unexpectedly" errors that occur
# when testcontainer Postgres stops before SQLAlchemy's pool flushes idle connections.
os.environ.setdefault("SQLALCHEMY_NULLPOOL", "1")
# Sessions default to a timezone that isn't UTC, as on the brain (Ubuntu's
# Postgres takes the machine's), so code that leans on UTC fails here first.
os.environ.setdefault("PGTZ", "America/New_York")
# The suite never talks to a real Quickwit: not the developer's (.env.local
# turns it on, and every metadata write would wait on a forced commit there
# and leave indexes behind), not any. Variables beat the env files in
# Settings. The URL is a closed port, so a test that turns Quickwit on
# without mocking the client fails at once instead of reaching one. Tests
# that need a real Quickwit are marked `quickwit` and start their own
# (tests/test_quickwit_real.py). `_quickwit_off` puts these back after
# each test.
os.environ["QUICKWIT_ENABLED"] = "false"
os.environ["QUICKWIT_URL"] = "http://127.0.0.1:9"
os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
# Nor does it lean on the developer's .env.local for the settings the app
# requires: a fresh checkout has none. The database URLs point at a host that
# can't resolve (tests that want a database start one and set their own), so a
# test that opens the database without one fails at once instead of reaching
# the developer's. Exported values still win (setdefault); .env.local doesn't,
# since variables beat the env files in Settings.
os.environ.setdefault("CONTROL_PLANE_DATABASE_URL", "postgresql://tests-start-their-own-db.invalid/control")
os.environ.setdefault(
    "TENANT_DATABASE_URL_TEMPLATE", "postgresql://tests-start-their-own-db.invalid/{tenant_id}"
)
os.environ.setdefault("JWT_SECRET", "lumiverb-test-jwt-secret")

# pyvips imports libvips via cffi.dlopen, which on macOS only searches the
# system dyld paths. uv's standalone Python builds do not have
# /opt/homebrew/lib on that search list, so contributors who installed
# libvips via Homebrew see ImportErrors at project time. Pre-populating
# DYLD_FALLBACK_LIBRARY_PATH from the Homebrew prefix at the top of the
# test session lets dlopen find the dylib without forcing every contributor
# to export the variable in their shell.
if sys.platform == "darwin":
    _existing_dyld = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    for _candidate_dir in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.isdir(_candidate_dir) and _candidate_dir not in _existing_dyld:
            _existing_dyld = (
                f"{_candidate_dir}:{_existing_dyld}" if _existing_dyld else _candidate_dir
            )
    if _existing_dyld:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = _existing_dyld

import psycopg2
import pytest
import testcontainers.postgres
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

_QUICKWIT_OFF = {k: os.environ[k] for k in ("QUICKWIT_ENABLED", "QUICKWIT_URL", "QUICKWIT_FALLBACK_TO_POSTGRES")}

# The Postgres image tests run against. Keep it the version production runs
# (scripts/deploy-api.sh); LUMIVERB_TEST_PG_IMAGE overrides it to try another.
PG_IMAGE = os.environ.get("LUMIVERB_TEST_PG_IMAGE", "pgvector/pgvector:pg18")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PG_PER_MODULE = os.environ.get("LUMIVERB_TEST_PG_PER_MODULE") == "1"
# Each tree's alembic ini, the revision it upgrades to and the variable its env.py reads.
_MIGRATIONS = {
    "control": ("alembic-control.ini", "head", "ALEMBIC_CONTROL_URL"),
    "tenant": ("alembic-tenant.ini", "heads", "ALEMBIC_TENANT_URL"),
}


def _ensure_psycopg2(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _alembic_upgrade(tree: str, url: str, project_root: str = _PROJECT_ROOT) -> None:
    ini, revision, variable = _MIGRATIONS[tree]
    env = os.environ.copy()
    env[variable] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, "upgrade", revision],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def _create_vector_extension(url: str) -> None:
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    engine.dispose()


def _run_control_migrations(url: str) -> None:
    if not _clone_migrated(url, "control"):
        _alembic_upgrade("control", url)


def _provision_tenant_db(tenant_url: str, project_root: str) -> None:
    if not _clone_migrated(tenant_url, "tenant"):
        _create_vector_extension(tenant_url)
        _alembic_upgrade("tenant", tenant_url, project_root)


# ---------------------------------------------------------------------------
# One Postgres server per run (see the module docstring)
# ---------------------------------------------------------------------------

_RealPostgresContainer = testcontainers.postgres.PostgresContainer


class _Server:
    """The run's Postgres. The process that started it holds its container."""

    def __init__(self, host: str, port: int, user: str, password: str, container=None) -> None:
        self.params = {"host": host, "port": port, "user": user, "password": password}
        self.container = container

    @classmethod
    def start(cls) -> "_Server":
        # A throwaway server needs no durability: no fsync, data in memory.
        # max_connections covers every worker's engines at once.
        container = _RealPostgresContainer(
            PG_IMAGE,
            command="-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
            " -c max_connections=1000",
            tmpfs={"/var/lib/postgresql": "rw"},
            shm_size="1g",
        )
        try:
            container.start()
            host, port = container.get_container_host_ip(), int(container.get_exposed_port(5432))
        except Exception:
            with contextlib.suppress(Exception):
                container.stop()
            raise
        return cls(host, port, container.username, container.password, container)

    def url(self, dbname: str, driver: str | None = "psycopg2") -> str:
        p = self.params
        return URL.create(
            f"postgresql+{driver}" if driver else "postgresql",
            username=p["user"],
            password=p["password"],
            host=p["host"],
            port=p["port"],
            database=dbname,
        ).render_as_string(hide_password=False)

    def connect(self, dbname: str = "postgres"):
        conn = psycopg2.connect(dbname=dbname, **self.params)
        conn.autocommit = True
        return conn

    def run(self, *statements: str) -> None:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
        finally:
            conn.close()


_server: _Server | None = None
_server_error: Exception | None = None  # why it didn't start, so later modules don't wait again
_handed_out: set[str] = set()  # the empty databases this process made
_templates: set[str] = set()  # templates known to be built
_current_item: pytest.Item | None = None


def _the_server() -> _Server:
    global _server, _server_error
    if _server is None:
        if _server_error is not None:
            raise RuntimeError("the tests' Postgres didn't start") from _server_error
        try:
            _server = _Server.start()
        except Exception as e:
            _server_error = e
            raise
    return _server


class _Database:
    """Stands in for testcontainers' PostgresContainer: a new, empty database
    on the run's server rather than a server of its own, dropped on exit."""

    _numbers = itertools.count(1)

    def __new__(cls, image: str = PG_IMAGE, *args: object, **kwargs: object):
        if image != PG_IMAGE or args or set(kwargs) - {"driver"}:
            return _RealPostgresContainer(image, *args, **kwargs)  # asked for something else
        return super().__new__(cls)

    def __init__(self, image: str = PG_IMAGE, driver: str | None = "psycopg2") -> None:
        self.dbname = f"lv_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}_{next(self._numbers)}"
        self.driver = driver

    def start(self) -> "_Database":
        server = _the_server()
        self.username, self.password = server.params["user"], server.params["password"]
        server.run(f'CREATE DATABASE "{self.dbname}"')
        _handed_out.add(self.dbname)
        return self

    def stop(self, *args: object, **kwargs: object) -> None:
        _handed_out.discard(self.dbname)
        _the_server().run(f'DROP DATABASE IF EXISTS "{self.dbname}" WITH (FORCE)')

    def __enter__(self) -> "_Database":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def get_connection_url(self, host: str | None = None, driver: object = "default") -> str:
        url = _the_server().url(self.dbname, self.driver if driver == "default" else driver)
        return url if host is None else make_url(url).set(host=host).render_as_string(hide_password=False)


if not _PG_PER_MODULE:
    testcontainers.postgres.PostgresContainer = _Database


def _template(tree: str) -> str:
    """The database every migrated `tree` database is cloned from: an empty
    one with pgvector, upgraded once per run. Under -n the first worker to
    need it builds it while the others wait on the lock."""
    name = f"lv_template_{tree}"
    if name in _templates:
        return name
    server = _the_server()
    conn = server.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (name,))
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is None:
                building = f"{name}_building"
                cur.execute(f'DROP DATABASE IF EXISTS "{building}" WITH (FORCE)')
                cur.execute(f'CREATE DATABASE "{building}"')
                _create_vector_extension(server.url(building))
                _alembic_upgrade(tree, server.url(building))
                cur.execute(f'ALTER DATABASE "{building}" RENAME TO "{name}"')
                # A template can't be cloned while anything is connected to it.
                cur.execute(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')
    finally:
        conn.close()  # and with it the lock
    _templates.add(name)
    return name


# Nothing in the database but what every new one has, and pgvector: no
# relations, schemas, functions or types of its own (objects below 16384 come
# with Postgres; an extension's own are its members), no other extension and
# no settings of its own, none of which a clone would keep.
_IS_EMPTY = """
SELECT NOT EXISTS (SELECT 1 FROM pg_class WHERE oid >= 16384)
   AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE oid >= 16384)
   AND NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname NOT IN ('plpgsql', 'vector'))
   AND NOT EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase
                   WHERE d.datname = current_database())
   AND NOT EXISTS (
       SELECT 1 FROM (SELECT 'pg_proc'::regclass AS cls, oid FROM pg_proc
                      UNION ALL SELECT 'pg_type'::regclass, oid FROM pg_type) o
       WHERE o.oid >= 16384 AND NOT EXISTS (
           SELECT 1 FROM pg_depend d WHERE d.classid = o.cls AND d.objid = o.oid AND d.deptype IN ('e', 'i')))
"""


def _clone_migrated(url: str, tree: str) -> bool:
    """Replace the empty database at `url` with a clone of `tree`'s template,
    if it's one this run handed out and the test isn't a migration test.
    False leaves the database for alembic to migrate."""
    if _server is None or (_current_item and _current_item.get_closest_marker("migration")):
        return False
    u = make_url(url)
    if u.database not in _handed_out or u.port != _server.params["port"]:
        return False
    conn = _server.connect(u.database)
    try:
        with conn.cursor() as cur:
            cur.execute(_IS_EMPTY)
            if not cur.fetchone()[0]:
                return False  # something's in it already: migrate it in place
    finally:
        conn.close()
    template = _template(tree)
    _server.run(
        f'DROP DATABASE "{u.database}" WITH (FORCE)',
        f'CREATE DATABASE "{u.database}" TEMPLATE "{template}"',
    )
    return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Remember the running test, for module fixtures that set up inside it."""
    global _current_item
    _current_item = item
    try:
        return (yield)
    finally:
        _current_item = None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    global _server
    shared = getattr(config, "workerinput", {}).get("lumiverb_pg")
    if shared:  # an xdist worker: the controller started the server
        _server = _Server(**shared)
    # Under -n, each module's tests run on one worker: they share module
    # fixtures, and some count on what an earlier test in the module did.
    if getattr(config.option, "dist", "no") == "load" and not _dist_chosen(config):
        config.option.dist = "loadfile"


def _dist_chosen(config: pytest.Config) -> bool:
    args = [
        *config.invocation_params.args,
        *os.environ.get("PYTEST_ADDOPTS", "").split(),
        *config.getini("addopts"),
    ]
    return any(a == "-d" or a.startswith("--dist") for a in args)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: list) -> None:
    """The xdist controller starts the one server its workers share."""
    if not _PG_PER_MODULE:
        try:
            _the_server()
        except Exception as e:  # each worker then tries for itself, and fails its DB tests
            print(f"lumiverb tests: could not start Postgres ({e})", file=sys.stderr)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node) -> None:
    if _server is not None:
        node.workerinput["lumiverb_pg"] = _server.params


def pytest_unconfigure(config: pytest.Config) -> None:
    if _server is not None and _server.container is not None:
        _server.container.stop()


class _AuthClient:
    """HTTP client that wraps TestClient and adds Authorization. Returns response without exiting."""

    def __init__(self, client: TestClient, api_key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def get(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.post(path, **kwargs)

    def delete(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.request("DELETE", path, **kwargs)


@pytest.fixture(autouse=True)
def _quickwit_off():
    """Some tests set QUICKWIT_* in os.environ by hand and leave it set; put
    the suite's values back so the next test (or module) doesn't inherit a
    Quickwit turned on."""
    yield
    changed = {k: v for k, v in _QUICKWIT_OFF.items() if os.environ.get(k) != v}
    if changed:
        os.environ.update(changed)
        from src.server.config import get_settings

        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_host_fstab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Library roots are checked against /etc/fstab: tests see none of this
    machine's mounts unless they write an fstab of their own."""
    from pathlib import Path

    from src.client.cli import roots

    monkeypatch.setattr(roots, "FSTAB", Path("/nonexistent/lumiverb-test-fstab"))
