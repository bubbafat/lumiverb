"""Control migration a7c3e5f9b1d2: the server admin is a person
(users.server_admin). Where the install has one account, its admins become
server admins, as every account admin had the install's settings until then;
with several, nobody does (the machine's owner names one). Runs Alembic
against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "9e4a6c8b2d1f", "a7c3e5f9b1d2"


def _install(url: str, accounts: list[tuple[str, str]], people: list[tuple[str, str, str]]) -> dict[str, bool]:
    """Accounts (tenant_id, status) and people (email, tenant_id, role) before; each person's server_admin after."""
    engine = create_engine(url)
    try:
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for tenant_id, status in accounts:
                conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                                  " VALUES (:t, :t, 'free', :s, now())"), {"t": tenant_id, "s": status})
            for email, tenant_id, role in people:
                conn.execute(text("INSERT INTO users (user_id, tenant_id, email, password_hash, role, created_at)"
                                  " VALUES (:e, :t, :e, 'x', :r, now())"), {"e": email, "t": tenant_id, "r": role})
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            return dict(conn.execute(text("SELECT email, server_admin FROM users")).all())
    finally:
        engine.dispose()


@pytest.mark.migration
def test_one_account_s_admins_become_server_admins_and_several_accounts_name_nobody() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        # A deleted account doesn't count: the install has one.
        assert _install(url, [("ten_mine", "active"), ("ten_old", "deleted")], [
            ("robert@example.com", "ten_mine", "admin"), ("partner@example.com", "ten_mine", "admin"),
            ("kid@example.com", "ten_mine", "viewer"), ("left@example.com", "ten_old", "admin"),
        ]) == {"robert@example.com": True, "partner@example.com": True, "kid@example.com": False,
               "left@example.com": False}
        _alembic(url, "downgrade", BEFORE)
        engine = create_engine(url)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")).all()}
        engine.dispose()
        assert "server_admin" not in columns

    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        assert _install(url, [("ten_a", "active"), ("ten_b", "active")], [
            ("a@example.com", "ten_a", "admin"), ("b@example.com", "ten_b", "admin"),
        ]) == {"a@example.com": False, "b@example.com": False}
