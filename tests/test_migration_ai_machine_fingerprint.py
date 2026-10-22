"""Control migration c5e9a3f7b1d4: an https AI machine's certificate
fingerprint (ai_machines.fingerprint), "" for every machine there is, so an
http machine stays as it is and an https one is checked as any https site
is; only ever an https machine's, in the one form it's kept in; a downgrade
drops it. Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "b3f7d1a9c5e8", "c5e9a3f7b1d4"
PINNED = ":".join(["7F"] * 32)


@pytest.mark.migration
def test_every_machine_comes_out_without_one_and_only_an_https_one_takes_one() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                              " VALUES ('ten_a', 'A', 'free', 'active', now())"))
            for machine_id, api_url in (("aim_ollama", "http://10.0.0.5:11434/v1"),
                                        ("aim_openai", "https://api.openai.com/v1")):
                conn.execute(text("INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, api_key, jobs,"
                                  " at_once, enabled, built_in, shares_gpu, status_error, models, created_at)"
                                  " VALUES (:m, 'ten_a', :m, :u, '', '[\"vision\"]', 2, true, false, false, '',"
                                  " '[]', now())"), {"m": machine_id, "u": api_url})
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT machine_id, fingerprint FROM ai_machines ORDER BY machine_id")).all() == [
                ("aim_ollama", ""), ("aim_openai", "")]
        for machine_id, wrong in (("aim_ollama", PINNED), ("aim_openai", PINNED.lower()),
                                  ("aim_openai", PINNED.replace(":", "")), ("aim_openai", "7F:7F")):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE ai_machines SET fingerprint = :f WHERE machine_id = :m"),
                             {"f": wrong, "m": machine_id})
        with engine.begin() as conn:
            conn.execute(text("UPDATE ai_machines SET fingerprint = :f WHERE machine_id = 'aim_openai'"), {"f": PINNED})

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_machines'")).all()}
            constraints = {r[0] for r in conn.execute(text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'ai_machines'::regclass")).all()}
        engine.dispose()
        assert "fingerprint" not in columns and "ai_machines_fingerprint" not in constraints
