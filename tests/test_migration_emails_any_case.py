"""Control migration b5d9e3a7f1c4: an email address is one person's, whatever
its case. Up, a unique index on the lowercased address; an install that
already has two users differing only in case keeps both and gets no index
(dropping either would lose an account); down, the index goes. Alembic
against throwaway databases only."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "d7f3b9e1c5a2", "b5d9e3a7f1c4"
INDEX = "SELECT count(*) FROM pg_indexes WHERE indexname = 'users_email_any_case'"


def _alembic(url: str, *args: str) -> str:
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-control.ini", *args], cwd=PROJECT_ROOT,
                            env={**os.environ, "ALEMBIC_CONTROL_URL": url}, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result.stdout + result.stderr


def _user(conn, user_id: str, email: str) -> None:
    conn.execute(text("INSERT INTO users (user_id, tenant_id, email, password_hash, role, server_admin, every_library,"
                      " token_version, created_at) VALUES (:u, 'ten_a', :e, 'x', 'admin', false, true, 0, now())"),
                 {"u": user_id, "e": email})


def _install(pg) -> tuple[str, object]:
    url = _ensure_psycopg2(pg.get_connection_url())
    _alembic(url, "upgrade", BEFORE)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, ai_job_models, created_at)"
                          " VALUES ('ten_a', 'a', 'free', 'active', '{}', now())"))
        _user(conn, "usr_a", "Ana@Example.com")
    return url, engine


def test_up_an_address_differing_only_in_case_is_refused_and_down_it_isnt() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url, engine = _install(pg)
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text(INDEX)).scalar_one() == 1
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _user(conn, "usr_b", "ana@example.com")

        _alembic(url, "downgrade", BEFORE)
        with engine.begin() as conn:
            assert conn.execute(text(INDEX)).scalar_one() == 0
            _user(conn, "usr_b", "ana@example.com")  # as before: unique only as typed
        engine.dispose()


def test_an_install_that_has_two_already_keeps_both_and_says_so() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url, engine = _install(pg)
        with engine.begin() as conn:
            _user(conn, "usr_b", "ana@example.com")
        said = _alembic(url, "upgrade", AFTER)
        assert "1 email address(es) belong to more than one user" in said, said
        with engine.connect() as conn:
            assert conn.execute(text(INDEX)).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 2, "nobody's account lost"
        engine.dispose()
