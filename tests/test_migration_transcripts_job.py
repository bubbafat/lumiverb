"""Control migration b6591b39778f: transcripts become an AI job. Every
tenant gets its built-in Whisper (the worker's own, no URL), doing
transcripts one at a time as the worker did, and the job's model starts at
small, what every transcript so far was made with. A downgrade takes them
away again. Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "8dfad06a4714", "b6591b39778f"


@pytest.mark.migration
def test_each_tenant_gets_its_built_in_whisper_and_small() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for tid in ("ten_a", "ten_b"):
                conn.execute(text(
                    "INSERT INTO tenants (tenant_id, name, plan, status, created_at, vision_model_id)"
                    " VALUES (:t, :t, 'free', 'active', now() - interval '1 day', 'qwen3-vl:8b')"), {"t": tid})
            conn.execute(text(
                "INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, jobs)"
                " VALUES ('aim_gpu', 'ten_a', 'Brain', 'http://10.10.10.2:11434/v1', '[\"vision\"]'),"
                "        ('aim_taken', 'ten_b', 'Built in', 'http://x/v1', '[]')"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            built_in = conn.execute(text(
                "SELECT tenant_id, name, api_url, api_key, jobs, at_once, enabled, online FROM ai_machines"
                " WHERE built_in ORDER BY tenant_id")).all()
            models = dict(conn.execute(text("SELECT tenant_id, transcript_model_id FROM tenants")).all())
            # Listed first: as old as its tenant.
            first = conn.execute(text(
                "SELECT machine_id FROM ai_machines WHERE tenant_id = 'ten_a' ORDER BY created_at LIMIT 1")).scalar()
        assert [tuple(r) for r in built_in] == [
            ("ten_a", "Built in", "", "", ["transcripts"], 1, True, None),
            # A machine already had the name.
            ("ten_b", "Built in (2)", "", "", ["transcripts"], 1, True, None),
        ]
        assert models == {"ten_a": "small", "ten_b": "small"}
        assert first != "aim_gpu"

        # One built-in each.
        with pytest.raises(Exception, match="uq_ai_machines_one_built_in"), engine.begin() as conn:
            conn.execute(text("INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, built_in)"
                              " VALUES ('aim_again', 'ten_a', 'Again', '', true)"))

        with engine.begin() as conn:
            conn.execute(text("UPDATE ai_machines SET jobs = '[\"vision\", \"transcripts\"]' WHERE machine_id = 'aim_gpu'"))
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            rows = dict(conn.execute(text("SELECT machine_id, jobs FROM ai_machines")).all())
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")).all()}
        assert rows == {"aim_gpu": ["vision"], "aim_taken": []}
        assert "transcript_model_id" not in columns
