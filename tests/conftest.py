"""Lumiverb test suite.

Shared helpers for slow tests (testcontainers + control/tenant DB setup):
- _ensure_psycopg2: normalize Postgres URL for SQLAlchemy
- _run_control_migrations: run alembic-control.ini upgrade head
- _provision_tenant_db: create vector extension + run alembic-tenant.ini upgrade head
- _AuthClient: TestClient wrapper that adds Authorization header to get/post
"""

import os
import subprocess
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


def _ensure_psycopg2(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _run_control_migrations(url: str) -> None:
    env = os.environ.copy()
    env["ALEMBIC_CONTROL_URL"] = url
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-control.ini", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def _provision_tenant_db(tenant_url: str, project_root: str) -> None:
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    engine.dispose()
    env = os.environ.copy()
    env["ALEMBIC_TENANT_URL"] = tenant_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic-tenant.ini", "upgrade", "heads"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


class _AuthClient:
    """HTTP client that wraps TestClient and adds Authorization. Returns response without exiting."""

    def __init__(self, client: TestClient, api_key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def get(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.get(path, **kwargs)

    def post(self, path: str, **kwargs: object) -> object:
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._headers)
        return self._client.post(path, **kwargs)
