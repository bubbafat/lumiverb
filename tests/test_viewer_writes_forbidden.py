"""A viewer can't write: every route that changes something answers a viewer 403.

Enumerates the app's routes, so a write added later without a role check
fails here. Viewer-writable routes are listed on purpose below.
"""

from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines

from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations

# Writes a viewer may make: their own ratings and saved views, and reads
# sent as POST (a body too big for a query string).
VIEWER_WRITABLE = {
    ("PUT", "/v1/assets/{asset_id}/rating"),
    ("PUT", "/v1/assets/ratings"),
    ("POST", "/v1/assets/ratings/lookup"),
    ("POST", "/v1/views"),
    ("PATCH", "/v1/views/reorder"),
    ("PATCH", "/v1/views/{view_id}"),
    ("DELETE", "/v1/views/{view_id}"),
    ("POST", "/v1/assets/state-check"),
    ("POST", "/v1/assets/project-usage"),
    ("POST", "/v1/similar/search-by-image"),
    ("POST", "/v1/similar/search-by-vector"),
}

# Reads that change something: claiming a video chunk takes it from the queue.
WRITING_GETS = {("GET", "/v1/video/{asset_id}/chunks/next")}


def _outside_tenant_auth(path: str) -> bool:
    """Signing in, the operator's admin key, and upkeep (another PR) aren't tenant roles."""
    return path.startswith(("/v1/auth/", "/v1/admin/", "/v1/upkeep"))


def _writes() -> list[tuple[str, str]]:
    out = []
    for r in app.routes:
        if not isinstance(r, APIRoute) or _outside_tenant_auth(r.path):
            continue
        for m in sorted(r.methods - {"GET", "HEAD", "OPTIONS"}):
            out.append((m, r.path))
        for m in r.methods & {"GET"}:
            if (m, r.path) in WRITING_GETS:
                out.append((m, r.path))
    return out


@pytest.fixture(scope="module")
def viewer_client():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer(PG_IMAGE) as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(make_url(control_url).set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post("/v1/admin/tenants", json={"name": "ViewerTenant", "plan": "free"},
                                headers={"Authorization": "Bearer test-admin-secret"})
                assert r.status_code == 200
                tenant_id, admin_key = r.json()["tenant_id"], r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                r = client.post("/v1/keys", json={"label": "viewer", "role": "viewer"},
                                headers={"Authorization": f"Bearer {admin_key}"})
                assert r.status_code == 200, r.text
                yield client, r.json()["plaintext"]
        _engines.clear()


def test_the_allowlist_names_real_routes() -> None:
    routes = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert VIEWER_WRITABLE <= routes
    assert WRITING_GETS <= routes


@pytest.mark.slow
def test_a_viewer_gets_403_on_every_write(viewer_client) -> None:
    client, viewer_key = viewer_client
    headers = {"Authorization": f"Bearer {viewer_key}"}
    let_through = []
    for method, path in _writes():
        if (method, path) in VIEWER_WRITABLE:
            continue
        url = re.sub(r"\{[^}]+\}", "x", path)
        r = client.request(method, url, json={}, headers=headers)
        if r.status_code != 403:
            let_through.append(f"{method} {path} -> {r.status_code}")
    assert not let_through, "Writes a viewer isn't refused:\n" + "\n".join(let_through)
