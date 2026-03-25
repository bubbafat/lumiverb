"""Path filters API integration tests. Uses testcontainers Postgres + tenant DB."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def path_filters_client() -> tuple[TestClient, str, str]:
    """
    Two testcontainers Postgres: control + tenant. Create tenant via admin API,
    provision tenant DB, return (client, admin_api_key, library_id).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = control_postgres.get_connection_url()
        control_url = _ensure_psycopg2(control_url)
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()

        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-path-filters"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "PathFiltersTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-path-filters"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                # Create a library to attach filters to
                cr = client.post(
                    "/v1/libraries",
                    json={"name": "FilterTestLib", "root_path": "/tmp"},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert cr.status_code == 200
                library_id = cr.json()["library_id"]
                yield client, api_key, library_id

        _engines.clear()


@pytest.mark.slow
def test_get_filters_empty(path_filters_client: tuple[TestClient, str, str]) -> None:
    """GET /v1/libraries/{id}/filters returns empty includes/excludes for new library."""
    client, api_key, library_id = path_filters_client
    r = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["includes"] == []
    assert data["excludes"] == []


@pytest.mark.slow
def test_post_filter_and_get(path_filters_client: tuple[TestClient, str, str]) -> None:
    """POST creates filter and it appears in subsequent GET."""
    client, api_key, library_id = path_filters_client
    r = client.post(
        f"/v1/libraries/{library_id}/filters",
        json={"type": "include", "pattern": "Photos/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["type"] == "include"
    assert created["pattern"] == "Photos/**"
    assert created["filter_id"].startswith("lpf_")

    r2 = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r2.status_code == 200
    data = r2.json()
    assert len(data["includes"]) == 1
    assert data["includes"][0]["pattern"] == "Photos/**"
    assert data["includes"][0]["filter_id"] == created["filter_id"]


@pytest.mark.slow
def test_delete_filter(path_filters_client: tuple[TestClient, str, str]) -> None:
    """DELETE removes filter; subsequent GET no longer includes it."""
    client, api_key, library_id = path_filters_client
    r = client.post(
        f"/v1/libraries/{library_id}/filters",
        json={"type": "exclude", "pattern": "**/Proxy/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 201
    filter_id = r.json()["filter_id"]

    r_del = client.delete(
        f"/v1/libraries/{library_id}/filters/{filter_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_del.status_code == 204

    r_get = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_get.status_code == 200
    assert not any(f["filter_id"] == filter_id for f in r_get.json()["excludes"])


@pytest.mark.slow
def test_post_filter_invalid_pattern_dot_dot(path_filters_client: tuple[TestClient, str, str]) -> None:
    """POST with .. in pattern returns 400."""
    client, api_key, library_id = path_filters_client
    r = client.post(
        f"/v1/libraries/{library_id}/filters",
        json={"type": "exclude", "pattern": "../etc/passwd"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 400


@pytest.mark.slow
def test_delete_unknown_filter_404(path_filters_client: tuple[TestClient, str, str]) -> None:
    """DELETE with unknown filter_id returns 404."""
    client, api_key, library_id = path_filters_client
    r = client.delete(
        f"/v1/libraries/{library_id}/filters/lpf_00000000000000000000000000",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_filters_require_admin(path_filters_client: tuple[TestClient, str, str]) -> None:
    """Viewer key on library filters endpoints returns 403."""
    import hashlib
    from src.core.database import get_control_session
    from sqlmodel import text as sql_text

    client, admin_key, library_id = path_filters_client
    # Create a key (inherits admin), then downgrade to viewer.
    r_key = client.post(
        "/v1/keys",
        json={"label": "viewer-filter-test"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert r_key.status_code == 200
    viewer_plaintext = r_key.json()["plaintext"]

    key_hash = hashlib.sha256(viewer_plaintext.encode()).hexdigest()
    with get_control_session() as session:
        session.exec(
            sql_text("UPDATE api_keys SET role = 'viewer' WHERE key_hash = :h"),
            params={"h": key_hash},
        )
        session.commit()

    r = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {viewer_plaintext}"},
    )
    assert r.status_code == 403


# --- Tenant filter defaults ---


@pytest.mark.slow
def test_tenant_defaults_empty(path_filters_client: tuple[TestClient, str, str]) -> None:
    """GET /v1/tenant/filter-defaults returns empty for new tenant."""
    client, api_key, _ = path_filters_client
    r = client.get(
        "/v1/tenant/filter-defaults",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["includes"] == []
    assert data["excludes"] == []


@pytest.mark.slow
def test_tenant_defaults_post_and_get(path_filters_client: tuple[TestClient, str, str]) -> None:
    """POST tenant default and GET returns it."""
    client, api_key, _ = path_filters_client
    r = client.post(
        "/v1/tenant/filter-defaults",
        json={"type": "exclude", "pattern": "**/Proxy/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["type"] == "exclude"
    assert created["pattern"] == "**/Proxy/**"
    assert created["default_id"].startswith("tpfd_")

    r2 = client.get(
        "/v1/tenant/filter-defaults",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r2.status_code == 200
    assert len(r2.json()["excludes"]) == 1
    assert r2.json()["excludes"][0]["default_id"] == created["default_id"]


@pytest.mark.slow
def test_library_inherits_tenant_defaults_on_creation(path_filters_client: tuple[TestClient, str, str]) -> None:
    """Library creation with existing tenant defaults: new library inherits those defaults."""
    client, api_key, library_id = path_filters_client
    # Add tenant defaults
    client.post(
        "/v1/tenant/filter-defaults",
        json={"type": "include", "pattern": "Photos/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    client.post(
        "/v1/tenant/filter-defaults",
        json={"type": "exclude", "pattern": "**/Proxy/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    # Create a new library
    r = client.post(
        "/v1/libraries",
        json={"name": "InheritedDefaultsLib", "root_path": "/tmp"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    new_lib_id = r.json()["library_id"]
    r_f = client.get(
        f"/v1/libraries/{new_lib_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_f.status_code == 200
    data = r_f.json()
    assert any(i["pattern"] == "Photos/**" for i in data["includes"])
    assert any(e["pattern"] == "**/Proxy/**" for e in data["excludes"])
    # That new library inherited defaults at creation. The "existing library unchanged"
    # behavior is covered by test_adding_defaults_after_library_creation_does_not_affect_existing.


@pytest.mark.slow
def test_adding_defaults_after_library_creation_does_not_affect_existing(path_filters_client: tuple[TestClient, str, str]) -> None:
    """Adding tenant defaults after library creation does not affect existing library."""
    client, api_key, library_id = path_filters_client
    # Library already exists with no filters (or whatever we added in other tests)
    r_before = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_before.status_code == 200
    # Add a tenant default now
    client.post(
        "/v1/tenant/filter-defaults",
        json={"type": "exclude", "pattern": "**/Cache/**"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    # Existing library should be unchanged
    r_after = client.get(
        f"/v1/libraries/{library_id}/filters",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r_after.status_code == 200
    # FilterTestLib was created at fixture time before this default existed; it should not gain it
    excludes = r_after.json()["excludes"]
    patterns = [e["pattern"] for e in excludes]
    assert "**/Cache/**" not in patterns
