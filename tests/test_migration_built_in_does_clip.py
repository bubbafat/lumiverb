"""Control migration c5e9a3b7d1f4 (ADR-022 phase 2): CLIP becomes an AI job,
and every account's built-in machine is given it, as the scheduler has
always made CLIP vectors itself; a machine with a URL isn't, nor is a
built-in machine given it twice. A downgrade takes it away again, nothing
else. Runs Alembic against a throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "b3d7f1a5c9e2", "c5e9a3b7d1f4"


@pytest.mark.migration
def test_each_built_in_machine_does_clip_and_nothing_else_changes() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for tid in ("ten_a", "ten_b"):
                conn.execute(text("INSERT INTO tenants (tenant_id, name, plan, status, created_at)"
                                  " VALUES (:t, :t, 'free', 'active', now())"), {"t": tid})
            conn.execute(text(
                "INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, jobs, built_in)"
                " VALUES ('aim_self_a', 'ten_a', 'Built in', '', '[\"transcripts\"]', true),"
                "        ('aim_self_b', 'ten_b', 'Built in', '', '[]', true),"
                "        ('aim_gpu', 'ten_a', 'Gaming PC', 'http://10.10.10.2:11434/v1', '[\"vision\"]', false)"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            jobs = {(r[0], r[1]): r[2] for r in conn.execute(text(
                "SELECT tenant_id, built_in, jobs FROM ai_machines ORDER BY tenant_id")).all()}
        expected = {("ten_a", False): ["vision"], ("ten_a", True): ["transcripts", "clip"], ("ten_b", True): ["clip"]}
        assert jobs == expected
        # Again: given once.
        _alembic(url, "downgrade", BEFORE)
        _alembic(url, "upgrade", AFTER)
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            again = {(r[0], r[1]): r[2] for r in conn.execute(text(
                "SELECT tenant_id, built_in, jobs FROM ai_machines ORDER BY tenant_id")).all()}
        assert again == expected
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            after_down = {(r[0], r[1]): r[2] for r in conn.execute(text(
                "SELECT tenant_id, built_in, jobs FROM ai_machines ORDER BY tenant_id")).all()}
        assert after_down == {k: [j for j in v if j != "clip"] for k, v in expected.items()}
