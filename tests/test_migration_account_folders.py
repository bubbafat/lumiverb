"""Control migration b3f7d1a9c5e8: each account's folders on this server
(tenants.folders), NULL for every account there is, which is every folder
the server reads, as before; a list when given, never another kind of
JSON; a downgrade drops them. Runs Alembic against a throwaway
testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "e8b4c2f6a0d3", "b3f7d1a9c5e8"


@pytest.mark.migration
def test_every_account_comes_out_reading_every_folder_the_server_reads() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for tenant_id in ("ten_a", "ten_b"):
                conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                                  " VALUES (:t, :t, 'free', 'active', now())"), {"t": tenant_id})
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT tenant_id, folders FROM tenants ORDER BY tenant_id")).all() == [
                ("ten_a", None), ("ten_b", None)]
        for wrong in ('"/Volumes/Ana"', '{"folder": "/Volumes/Ana"}', "null"):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE tenants SET folders = CAST(:f AS jsonb)"), {"f": wrong})
        with engine.begin() as conn:
            conn.execute(text("UPDATE tenants SET folders = '[\"/Volumes/Ana\"]' WHERE tenant_id = 'ten_a'"))
            conn.execute(text("UPDATE tenants SET folders = '[]' WHERE tenant_id = 'ten_b'"))

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")).all()}
            constraints = {r[0] for r in conn.execute(text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'tenants'::regclass")).all()}
        engine.dispose()
        assert "folders" not in columns and "tenants_folders" not in constraints
