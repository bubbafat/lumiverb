"""Control migration 8dfad06a4714: a tenant's one vision endpoint becomes
its first AI machine, doing descriptions & text, two at once; a downgrade
puts it back. Runs Alembic against a throwaway testcontainers database only."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from tests.conftest import PG_IMAGE, _ensure_psycopg2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEFORE, AFTER = "i5c6d7e8f9a0", "8dfad06a4714"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_CONTROL_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic-control.ini", *args],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.migration
def test_the_vision_endpoint_becomes_the_first_machine_and_comes_back() -> None:
    with PostgresContainer(PG_IMAGE) as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        engine = create_engine(url)
        _alembic(url, "upgrade", BEFORE)
        with engine.begin() as conn:
            for tid, api_url, key in (("ten_a", "http://172.18.0.6:11434/v1/", "sk-a"), ("ten_b", "", "")):
                conn.execute(text(
                    "INSERT INTO tenants (tenant_id, name, plan, status, created_at, vision_api_url, vision_api_key,"
                    " vision_model_id) VALUES (:t, :t, 'free', 'active', now(), :u, :k, 'qwen3-vl:8b')"
                ), {"t": tid, "u": api_url, "k": key})
        _alembic(url, "upgrade", AFTER)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT tenant_id, name, api_url, api_key, jobs, at_once, enabled, online FROM ai_machines")).all()
            columns = {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tenants'")).all()}
        assert [tuple(r) for r in rows] == [
            ("ten_a", "172.18.0.6:11434", "http://172.18.0.6:11434/v1", "sk-a", ["vision"], 2, True, None)]
        assert "vision_api_url" not in columns and "vision_model_id" in columns

        _alembic(url, "downgrade", BEFORE)
        with engine.connect() as conn:
            back = dict(conn.execute(text("SELECT tenant_id, vision_api_url FROM tenants")).all())
            key = conn.execute(text("SELECT vision_api_key FROM tenants WHERE tenant_id = 'ten_a'")).scalar()
        assert back == {"ten_a": "http://172.18.0.6:11434/v1", "ten_b": ""} and key == "sk-a"
