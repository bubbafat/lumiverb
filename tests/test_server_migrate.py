"""lumiverb-server migrate (src/server/lifecycle/migrate.py): scripts/migrate.sh's
steps, as a function a Mac's service runs before it starts. On real Postgres:
from nothing to head, again with nothing to do, and an account whose
database fails is named while the others are still migrated."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlmodel import Session
from testcontainers.postgres import PostgresContainer

from src.server.lifecycle import cli
from src.server.lifecycle.migrate import APP_DIR, MigrationError, migrate
from tests.conftest import PG_IMAGE, _ensure_psycopg2

pytestmark = pytest.mark.slow


def _head(ini: str) -> str:
    config = Config(str(APP_DIR / ini))
    config.set_main_option("script_location", str(APP_DIR / config.get_main_option("script_location")))
    return ScriptDirectory.from_config(config).get_current_head()


def _version(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            exists = conn.execute(text("SELECT to_regclass('alembic_version')")).scalar()
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar() if exists else None
    finally:
        engine.dispose()


def _with_vector(url: str) -> str:
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    engine.dispose()
    return url


def _add_tenant(control_url: str, tenant_id: str, tenant_url: str) -> None:
    from src.server.models.control_plane import Tenant, TenantDbRouting

    engine = create_engine(control_url)
    with Session(engine) as session:
        session.add(Tenant(tenant_id=tenant_id, name=tenant_id))
        session.flush()
        session.add(TenantDbRouting(tenant_id=tenant_id, connection_string=tenant_url))
        session.commit()
    engine.dispose()


@pytest.fixture
def databases():
    with PostgresContainer(PG_IMAGE) as control, PostgresContainer(PG_IMAGE) as one, PostgresContainer(PG_IMAGE) as two:
        yield [_with_vector(_ensure_psycopg2(pg.get_connection_url())) for pg in (control, one, two)]


def test_from_nothing_to_head_then_nothing_to_do(databases):
    control_url, one_url, two_url = databases
    said: list[str] = []

    assert migrate(control_url, say=said.append) == 0  # no accounts yet
    assert _version(control_url) == _head("alembic-control.ini")

    _add_tenant(control_url, "ten_one", one_url)
    _add_tenant(control_url, "ten_two", two_url)
    assert migrate(control_url, say=said.append) == 2
    tenant_head = _head("alembic-tenant.ini")
    assert _version(one_url) == tenant_head and _version(two_url) == tenant_head
    assert "  [2/2] ten_two ✓" in said

    assert migrate(control_url, say=lambda _: None) == 2  # again: nothing to do, no error


def test_an_account_that_fails_is_named_and_the_others_still_migrate(databases):
    control_url, one_url, _ = databases
    migrate(control_url, say=lambda _: None)
    _add_tenant(control_url, "ten_gone", "postgresql+psycopg2://nobody:x@127.0.0.1:1/nothing")
    _add_tenant(control_url, "ten_ok", one_url)

    with pytest.raises(MigrationError, match=r"1 of 2 accounts' migrations failed: ten_gone: "):
        migrate(control_url, say=lambda _: None)
    assert _version(one_url) == _head("alembic-tenant.ini")


def test_the_command_reads_the_install_env_file(databases, tmp_path, monkeypatch, capsys):
    control_url, *_ = databases
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)
    env_file = tmp_path / "env"
    env_file.write_text(f"# the install's\nCONTROL_PLANE_DATABASE_URL={control_url}\n")

    assert cli.main(["--env", str(env_file), "migrate"]) == 0
    assert "✓ Migrated the control plane and 0 account(s)" in capsys.readouterr().out
    assert _version(control_url) == _head("alembic-control.ini")


def test_the_command_says_what_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CONTROL_PLANE_DATABASE_URL", raising=False)
    assert cli.main(["--env", str(tmp_path / "nowhere"), "migrate"]) == 1
    assert "No CONTROL_PLANE_DATABASE_URL in" in capsys.readouterr().err
