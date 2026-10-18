# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Roles per library (ADR-018 decision 8, phase 8): the rule that gives a
person their role on each library (src/server/repository/library_roles.py),
against a real account database; the migrations that store it; and a
person's roles going when they do."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.repository.library_roles import LibraryRoleRepository, Person
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db
from tests.test_scans_api import _library, _ok, env  # noqa: F401 — the shared fixture

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture(scope="module")
def account():
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        _provision_tenant_db(url, PROJECT_ROOT)
        engine = create_engine(url)
        with engine.begin() as conn:
            for library_id, private in (("lib_a", False), ("lib_b", False), ("lib_c", False), ("lib_p", True)):
                conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, private, created_at,"
                                  " updated_at) VALUES (:l, :l, '/' || :l, 'active', :p, now(), now())"),
                             {"l": library_id, "p": private})
        try:
            yield engine
        finally:
            engine.dispose()


def _set(engine, library_id: str, user_id: str, role: str | None) -> None:
    with Session(engine) as session:
        LibraryRoleRepository(session).set(library_id, user_id, role, by="usr_admin", now=NOW)
        session.commit()


def _roles(engine, person: Person) -> dict[str, str]:
    with Session(engine) as session:
        return LibraryRoleRepository(session).roles_of(person)


@pytest.mark.slow
def test_everyone_keeps_their_account_role_on_every_library_until_set_otherwise(account):
    for role in ("viewer", "editor"):
        assert _roles(account, Person("usr_nobody_set", role)) == {
            "lib_a": role, "lib_b": role, "lib_c": role, "lib_p": "none"}, "a private library: only its people"
    assert set(_roles(account, Person("usr_admin", "admin")).values()) == {"admin"}, "an account admin's: every one"


@pytest.mark.slow
def test_a_role_set_on_a_library_is_theirs_there_higher_or_lower(account):
    _set(account, "lib_a", "usr_ed", "editor")
    _set(account, "lib_b", "usr_ed", "none")
    _set(account, "lib_c", "usr_ed", "admin")
    _set(account, "lib_p", "usr_ed", "viewer")
    viewer = Person("usr_ed", "viewer")
    assert _roles(account, viewer) == {"lib_a": "editor", "lib_b": "none", "lib_c": "admin", "lib_p": "viewer"}
    with Session(account) as session:
        repo = LibraryRoleRepository(session)
        assert repo.role_on(viewer, "lib_b") == "none" and repo.role_on(viewer, "lib_nope") is None
        assert [(r["user_id"], r["role"], r["set_by"]) for r in repo.set_for("lib_c")] == [
            ("usr_ed", "admin", "usr_admin")]
    _set(account, "lib_b", "usr_ed", None)  # the rule's again
    assert _roles(account, viewer)["lib_b"] == "viewer"
    _set(account, "lib_a", "usr_ed", "none")
    assert _roles(account, Person("usr_ed", "admin"))["lib_a"] == "admin", "an account admin is every library's"
    assert _roles(account, Person("usr_someone_else", "viewer"))["lib_a"] == "viewer", "set for them alone"


@pytest.mark.slow
def test_someone_who_sees_only_what_theyre_given_sees_nothing_else(account):
    _set(account, "lib_b", "usr_guest", "editor")
    guest = Person("usr_guest", "editor", every_library=False)
    assert _roles(account, guest) == {"lib_a": "none", "lib_b": "editor", "lib_c": "none", "lib_p": "none"}


@pytest.mark.slow
def test_a_key_a_person_made_is_never_above_its_own_role(account):
    _set(account, "lib_a", "usr_keys", "admin")
    _set(account, "lib_b", "usr_keys", "none")
    key = Person("usr_keys", "editor", cap="viewer")
    assert _roles(account, key) == {"lib_a": "viewer", "lib_b": "none", "lib_c": "viewer", "lib_p": "none"}
    assert set(_roles(account, Person("usr_admin", "admin", cap="editor")).values()) == {"editor"}


@pytest.mark.slow
def test_only_the_four_roles_and_they_go_with_their_library(account):
    with Session(account) as session, pytest.raises(ValueError):
        LibraryRoleRepository(session).set("lib_a", "usr_x", "owner", by=None, now=NOW)
    with pytest.raises(IntegrityError), account.begin() as conn:
        conn.execute(text("INSERT INTO library_roles (library_id, user_id, role) VALUES ('lib_a', 'usr_x', 'owner')"))
    with pytest.raises(ValueError), Session(account) as session:
        LibraryRoleRepository(session).roles_of(Person("usr_x", "none"))  # an account role is never "none"
    with account.begin() as conn:
        conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                          " VALUES ('lib_gone', 'g', '/g', 'active', now(), now())"))
    _set(account, "lib_gone", "usr_x", "editor")
    with account.begin() as conn:
        conn.execute(text("DELETE FROM libraries WHERE library_id = 'lib_gone'"))
        assert conn.execute(text("SELECT count(*) FROM library_roles WHERE library_id = 'lib_gone'")).scalar() == 0


@pytest.mark.slow
def test_a_persons_roles_go_when_they_do(env):
    person = _ok(env.client.post("/v1/users", headers=env.admin, json={
        "email": "leaving@example.com", "password": "a long enough password", "role": "viewer"}), 201)
    library_id = _library(env)
    with Session(env.db) as session:
        LibraryRoleRepository(session).set(library_id, person["user_id"], "editor", by=None, now=NOW)
        session.commit()
    assert env.client.delete(f"/v1/users/{person['user_id']}", headers=env.admin).status_code == 204
    with env.db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM library_roles WHERE user_id = :u"),
                            {"u": person["user_id"]}).scalar() == 0


# ---------------------------------------------------------------------------
# The migrations
# ---------------------------------------------------------------------------


def _alembic(tree: str, url: str, *args: str) -> None:
    env = {**os.environ, f"ALEMBIC_{tree.upper()}_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", f"alembic-{tree}.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})}


@pytest.mark.migration
def test_the_account_migration_sets_nothing_and_a_downgrade_drops_it() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _alembic("tenant", url, "upgrade", "f2b6d8a4c1e9")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
                              " VALUES ('lib_a', 'a', '/a', 'active', now(), now())"))
        _alembic("tenant", url, "upgrade", "a3c7e1f5b9d2")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT private FROM libraries")).scalar() is False
            assert conn.execute(text("SELECT count(*) FROM library_roles")).scalar() == 0
            conn.execute(text("INSERT INTO library_roles (library_id, user_id, role) VALUES ('lib_a', 'usr_1', 'none')"))
        _alembic("tenant", url, "downgrade", "f2b6d8a4c1e9")
        assert "private" not in _columns(engine, "libraries") and not _columns(engine, "library_roles")
        engine.dispose()


@pytest.mark.migration
def test_the_control_migration_lets_everyone_see_every_library_and_a_downgrade_drops_it() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic("control", url, "upgrade", "c9e5a7b3d1f4")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, ai_job_models, created_at)"
                              " VALUES ('ten_1', 't', 'free', 'active', '{}', now())"))
            conn.execute(text("INSERT INTO users (user_id, tenant_id, email, password_hash, role, server_admin,"
                              " token_version, created_at) VALUES ('usr_1', 'ten_1', 'a@example.com', 'x', 'viewer',"
                              " false, 0, now())"))
        _alembic("control", url, "upgrade", "d4f8b2a6c0e3")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT every_library FROM users")).scalar() is True
        _alembic("control", url, "downgrade", "c9e5a7b3d1f4")
        assert "every_library" not in _columns(engine, "users")
        engine.dispose()
