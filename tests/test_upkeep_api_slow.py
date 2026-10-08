"""Upkeep endpoints called with a tenant API key (what `lumiverb maintenance` sends).

Upkeep routes skip the tenant middleware, so a handler can't rely on
request.state for the tenant; it resolves the tenant from the key itself.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yields (client, tenant_api_key)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tmp_path_factory.mktemp("upkeep_data")

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(
            make_url(control_url).set(database="{tenant_id}")
        )
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["DATA_DIR"] = str(data_dir)
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap:
                r = bootstrap.post(
                    "/v1/admin/tenants",
                    json={"name": "UpkeepTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                yield client, api_key

    os.environ.pop("DATA_DIR", None)
    get_settings.cache_clear()
    _engines.clear()


@pytest.mark.slow
def test_cleanup_with_tenant_key_runs_dry_run(env) -> None:
    client, api_key = env

    r = client.post(
        "/v1/upkeep/cleanup", params={"dry_run": "true"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["orphan_files"] == 0


@pytest.mark.slow
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-key"}])
def test_cleanup_without_valid_key_is_401(env, headers) -> None:
    client, _ = env

    r = client.post("/v1/upkeep/cleanup", headers=headers)

    assert r.status_code == 401, r.text


def _new_key(client, api_key: str, role: str) -> tuple[str, str]:
    r = client.post(
        "/v1/keys", json={"label": f"{role}-key", "role": role},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["key_id"], r.json()["plaintext"]


@pytest.mark.slow
@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_cleanup_needs_an_admin_key(env, role) -> None:
    """Cleanup deletes files: a viewer or editor key can't run it."""
    client, api_key = env
    _, key = _new_key(client, api_key, role)

    r = client.post(
        "/v1/upkeep/cleanup", params={"dry_run": "false"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert r.status_code == 403, r.text


@pytest.mark.slow
def test_cleanup_with_revoked_key_is_401(env) -> None:
    client, api_key = env
    key_id, key = _new_key(client, api_key, "admin")
    r = client.delete(f"/v1/keys/{key_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code in (200, 204), r.text

    r = client.post("/v1/upkeep/cleanup", headers={"Authorization": f"Bearer {key}"})

    assert r.status_code == 401, r.text
