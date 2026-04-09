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

# Use NullPool for all test engines so connections are never held idle.
# This prevents "server closed the connection unexpectedly" errors that occur
# when testcontainer Postgres stops before SQLAlchemy's pool flushes idle connections.
os.environ.setdefault("SQLALCHEMY_NULLPOOL", "1")

# pyvips imports libvips via cffi.dlopen, which on macOS only searches the
# system dyld paths. uv's standalone Python builds do not have
# /opt/homebrew/lib on that search list, so contributors who installed
# libvips via Homebrew see ImportErrors at collection time. Pre-populating
# DYLD_FALLBACK_LIBRARY_PATH from the Homebrew prefix at the top of the
# test session lets dlopen find the dylib without forcing every contributor
# to export the variable in their shell.
if sys.platform == "darwin":
    _existing_dyld = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    for _candidate_dir in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.isdir(_candidate_dir) and _candidate_dir not in _existing_dyld:
            _existing_dyld = (
                f"{_candidate_dir}:{_existing_dyld}" if _existing_dyld else _candidate_dir
            )
    if _existing_dyld:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = _existing_dyld

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
