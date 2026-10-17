"""Control migration c9e5a7b3d1f4: an account's key-idle days
(tenants.key_idle_days), off for every account until an admin sets them,
1 to 3,650 when set; a downgrade drops them. Runs Alembic against a
throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "a7c3e5f9b1d2", "c9e5a7b3d1f4"


@pytest.mark.migration
def test_every_account_starts_with_keys_kept_until_revoked() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                              " VALUES ('ten_a', 'a', 'free', 'active', now())"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT key_idle_days FROM tenants")).scalar() is None
        for days in (0, 3651):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE tenants SET key_idle_days = :d"), {"d": days})
        with engine.begin() as conn:
            conn.execute(text("UPDATE tenants SET key_idle_days = 90"))
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")).all()}
        engine.dispose()
        assert "key_idle_days" not in columns
