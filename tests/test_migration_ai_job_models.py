"""Control migration 6ab1e1dd3ef7: each AI job's model kept by the job's name.

tenants.ai_job_models ({job: model}) replaces vision_model_id and
transcript_model_id; every tenant's two models are carried over as they
are ("" stays off), and going down puts them back. Runs Alembic against a
throwaway testcontainers database only."""

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2
from tests.test_migration_ai_machines import _alembic

BEFORE, AFTER = "3f9c1d2e7a64", "6ab1e1dd3ef7"


@pytest.mark.migration
def test_each_jobs_model_is_kept_by_its_name_and_comes_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO tenants (tenant_id, name, plan, status, created_at, vision_model_id, transcript_model_id)"
                " VALUES ('ten_a', 'a', 'free', 'active', now(), 'qwen3-vl:8b', 'large-v3'),"
                "        ('ten_b', 'b', 'free', 'active', now(), '', 'small')"))
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            models = dict(conn.execute(text("SELECT tenant_id, ai_job_models FROM tenants")).all())
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")).all()}
        assert models == {"ten_a": {"vision": "qwen3-vl:8b", "transcripts": "large-v3"},
                          "ten_b": {"vision": "", "transcripts": "small"}}
        assert not columns & {"vision_model_id", "transcript_model_id"}
        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            back = {r[0]: (r[1], r[2]) for r in conn.execute(text(
                "SELECT tenant_id, vision_model_id, transcript_model_id FROM tenants"))}
        assert back == {"ten_a": ("qwen3-vl:8b", "large-v3"), "ten_b": ("", "small")}
        engine.dispose()
