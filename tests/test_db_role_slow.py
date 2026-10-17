"""A Mac install's database role, as Linux's (src/server/lifecycle/roles.py),
on real PostgreSQL 18: an install whose every database and object the
superuser owns (as initdb's bootstrap user made them) is handed to an
ordinary role that may only log in and make databases; then that role
migrates, writes, makes an account's database (with `vector`, from
template1), backs up and restores, and can't do what only a superuser may.
Each test's role is its own (roles are the server's, shared by every test)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from src.server.lifecycle.migrate import migrate, pending
from src.server.lifecycle.roles import ensure_app_role
from tests.conftest import _the_server

pytestmark = pytest.mark.slow


def _run(url: str, *statements: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _rows(url: str, sql: str, **params) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).all()
    finally:
        engine.dispose()


@pytest.fixture
def install():
    """An install as a Mac's was: the control plane and one account, made and
    migrated by the superuser, which owns them; and the role it's handed to."""
    from src.server.models.control_plane import Tenant, TenantDbRouting

    srv = _the_server()
    tag = f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}_{uuid.uuid4().hex[:6]}"
    control, tenant, role = f"cp_{tag}", f"ten_{tag}", f"lv_app_{tag}"
    superuser = srv.url("postgres")
    for name in (control, tenant):
        srv.run(f'CREATE DATABASE "{name}"')
        _run(srv.url(name), "CREATE EXTENSION IF NOT EXISTS vector")
    control_url, tenant_url = srv.url(control), srv.url(tenant)
    migrate(control_url, say=lambda _: None)
    engine = create_engine(control_url)
    with Session(engine) as session:
        session.add(Tenant(tenant_id="ten_a", name="A"))
        session.flush()
        session.add(TenantDbRouting(tenant_id="ten_a", connection_string=tenant_url))
        session.commit()
    engine.dispose()
    migrate(control_url, say=lambda _: None)
    # What the schema doesn't have but an install's might: a view, a type, a function.
    _run(tenant_url, "CREATE TYPE mood AS ENUM ('calm', 'busy')",
         "CREATE VIEW clip_count AS SELECT count(*) AS n FROM assets",
         "CREATE FUNCTION twice(x int) RETURNS int LANGUAGE sql AS 'SELECT x * 2'")
    app = make_url(control_url).set(username=role, password="app-pw-" + tag).render_as_string(hide_password=False)
    yield {"superuser": superuser, "control": control, "tenant": tenant, "app": app, "role": role}
    for name in (control, tenant, f"new_{tag}"):
        srv.run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    srv.run(f'DROP OWNED BY "{role}"', f'DROP ROLE IF EXISTS "{role}"')


def _as_app(info: dict, database: str) -> str:
    return make_url(info["app"]).set(database=database).render_as_string(hide_password=False)


def _not_owned(info: dict, database: str) -> list:
    """What in the database isn't the role's, but for what extensions own."""
    return _rows(_as_app(info, database), """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'i')
          AND pg_get_userbyid(c.relowner) <> current_user
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e')
        UNION ALL
        SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype = 'e' AND pg_get_userbyid(t.typowner) <> current_user
        UNION ALL
        SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND pg_get_userbyid(p.proowner) <> current_user
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')""")


def test_everything_the_superuser_owned_is_handed_to_the_role(install):
    done = ensure_app_role(install["superuser"], install["app"])
    assert done["role_made"] == 1 and done["handed_over"] > 50 and done["rerouted"] == 1

    for database in (install["control"], install["tenant"]):
        owner = _rows(install["superuser"], "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d",
                      d=database)[0][0]
        assert owner == install["role"]
        assert _not_owned(install, database) == []
    [(route,)] = _rows(_as_app(install, install["control"]), "SELECT connection_string FROM tenant_db_routing")
    assert (make_url(route).username, make_url(route).database) == (install["role"], install["tenant"])
    assert make_url(route).password == make_url(install["app"]).password


def test_the_role_may_log_in_and_make_databases_and_nothing_more(install):
    ensure_app_role(install["superuser"], install["app"])
    [(superuser, createdb, createrole, replication, bypassrls)] = _rows(
        install["superuser"], "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                              "FROM pg_roles WHERE rolname = :r", r=install["role"])
    assert (superuser, createdb, createrole, replication, bypassrls) == (False, True, False, False, False)
    with pytest.raises(SQLAlchemyError, match="permission denied|must be superuser|pg_execute_server_program"):
        _run(_as_app(install, install["tenant"]), "COPY (SELECT 1) TO PROGRAM 'true'")


def test_as_the_role_it_migrates_writes_and_makes_an_account_s_database(install):
    ensure_app_role(install["superuser"], install["app"])
    control = _as_app(install, install["control"])
    assert not pending(control)
    migrate(control, say=lambda _: None)  # as the role: nothing to do, nothing refused
    tenant = _as_app(install, install["tenant"])
    _run(tenant, "CREATE TABLE made_by_the_role (x int)", "INSERT INTO made_by_the_role VALUES (1)",
         "ALTER TABLE assets ADD COLUMN role_check int", "ALTER TABLE assets DROP COLUMN role_check",
         "SELECT twice(2)", "SELECT n FROM clip_count")
    new = make_url(install["app"]).set(database="postgres").render_as_string(hide_password=False)
    new_db = install["control"].replace("cp_", "new_")
    _run(new, f'CREATE DATABASE "{new_db}"')
    assert _rows(_as_app(install, new_db), "SELECT extname FROM pg_extension WHERE extname = 'vector'") == [("vector",)]


def test_a_second_start_changes_nothing(install):
    ensure_app_role(install["superuser"], install["app"])
    assert ensure_app_role(install["superuser"], install["app"]) == {"role_made": 0, "handed_over": 0, "rerouted": 0}


def test_the_role_s_password_is_the_env_file_s(install):
    ensure_app_role(install["superuser"], install["app"])
    changed = make_url(install["app"]).set(password="a-new-one").render_as_string(hide_password=False)
    done = ensure_app_role(install["superuser"], changed)
    assert done["rerouted"] == 1, "the routing follows"
    assert _rows(make_url(changed).set(database=install["tenant"]).render_as_string(hide_password=False),
                 "SELECT current_user") == [(install["role"],)]


def test_a_new_install_has_no_control_database_yet(install):
    srv = _the_server()
    srv.run(f'DROP DATABASE "{install["control"]}" WITH (FORCE)')
    done = ensure_app_role(install["superuser"], install["app"])
    assert done["role_made"] == 1 and done["rerouted"] == 0
