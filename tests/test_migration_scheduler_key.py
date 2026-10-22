"""Control migration e8b4c2f6a0d3: the scheduler's own key is known by a
mark only it gets (api_keys.scheduler), not by its label. The keys the old
rule took for the scheduler's (labelled "scheduler", made by no person, no
device's) are marked, and nothing else; a person's key or a device's can
never carry the mark; a downgrade drops it. Runs Alembic against a
throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "d1f5a9c3e7b2", "e8b4c2f6a0d3"

# key_id: (label, created_by_user_id, device_id)
KEYS = {
    "key_scheduler": ("scheduler", None, None),
    "key_persons": ("scheduler", "usr_a", None),
    "key_devices": ("scheduler", None, "dev_a"),
    "key_first": ("default", None, None),
}


@pytest.mark.migration
def test_the_keys_the_old_rule_took_for_the_scheduler_s_are_marked_and_nothing_else() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                              " VALUES ('ten_a', 'a', 'free', 'active', now())"))
            for key_id, (label, maker, device) in KEYS.items():
                conn.execute(text(
                    "INSERT INTO api_keys (key_id, key_hash, tenant_id, name, label, scopes, role, created_by_user_id,"
                    " device_id, created_at) VALUES (:k, :k, 'ten_a', :l, :l, '[]', :r, :m, :d, now())"),
                    {"k": key_id, "l": label, "m": maker, "d": device, "r": "device" if device else "admin"})
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            marked = dict(conn.execute(text("SELECT key_id, scheduler FROM api_keys")).all())
        assert marked == {"key_scheduler": True, "key_persons": False, "key_devices": False, "key_first": False}
        for key_id in ("key_persons", "key_devices"):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE api_keys SET scheduler = true WHERE key_id = :k"), {"k": key_id})
        with engine.begin() as conn:  # a new key is no scheduler's unless it says so
            conn.execute(text("INSERT INTO api_keys (key_id, key_hash, tenant_id, name, scopes, role, created_at)"
                              " VALUES ('key_new', 'key_new', 'ten_a', 'n', '[]', 'admin', now())"))
            assert conn.execute(text("SELECT scheduler FROM api_keys WHERE key_id = 'key_new'")).scalar() is False

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'api_keys'")).all()}
            constraints = {r[0] for r in conn.execute(text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'api_keys'::regclass")).all()}
        engine.dispose()
        assert "scheduler" not in columns and "api_keys_scheduler" not in constraints
