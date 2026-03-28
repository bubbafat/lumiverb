"""Phase 3 middleware tests: public library resolution without auth."""

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
def public_lib_client():
    """
    Two-container setup: control plane + tenant DB.
    Creates a tenant via admin API, provisions tenant DB, creates a library, and
    makes it public. Returns (client, api_key, library_id).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        engine = create_engine(control_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        engine.dispose()
        _run_control_migrations(control_url)

        u = make_url(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(u.set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "PubMiddlewareTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.core.database import get_control_session
            from src.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            auth = {"Authorization": f"Bearer {api_key}"}

            with TestClient(app) as client:
                # Create a public library
                r = client.post(
                    "/v1/libraries",
                    json={"name": "PublicLib", "root_path": "/pub"},
                    headers=auth,
                )
                assert r.status_code == 200, r.text
                library_id = r.json()["library_id"]

                r = client.patch(
                    f"/v1/libraries/{library_id}",
                    json={"is_public": True},
                    headers=auth,
                )
                assert r.status_code == 200, r.text

                # Create a private library for negative tests
                r = client.post(
                    "/v1/libraries",
                    json={"name": "PrivateLib", "root_path": "/priv"},
                    headers=auth,
                )
                assert r.status_code == 200, r.text
                private_library_id = r.json()["library_id"]

                yield client, api_key, library_id, private_library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Authenticated path: still works, is_public_request=False
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_authenticated_request_resolves_tenant(public_lib_client) -> None:
    """Authenticated GET /v1/libraries returns 200."""
    client, api_key, library_id, _ = public_lib_client
    r = client.get("/v1/libraries", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unauthenticated GET: public library via path param
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_get_public_library_returns_200(public_lib_client) -> None:
    """GET /v1/libraries/{id} for a public library without auth → 200."""
    client, _, library_id, _ = public_lib_client
    r = client.get(f"/v1/libraries/{library_id}")
    assert r.status_code == 200
    assert r.json()["is_public"] is True


@pytest.mark.slow
def test_unauthenticated_get_private_library_returns_401(public_lib_client) -> None:
    """GET /v1/libraries/{id} for a private library without auth → 401 from middleware."""
    client, _, _, private_library_id = public_lib_client
    r = client.get(f"/v1/libraries/{private_library_id}")
    assert r.status_code == 401


@pytest.mark.slow
def test_unauthenticated_get_directories_public_library(public_lib_client) -> None:
    """GET /v1/libraries/{id}/directories for a public library without auth → 200."""
    client, _, library_id, _ = public_lib_client
    r = client.get(f"/v1/libraries/{library_id}/directories")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unauthenticated GET: public library via library_id query param
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_assets_page_public_library(public_lib_client) -> None:
    """GET /v1/assets/page?library_id= for a public library without auth → 200 (empty items)."""
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/page", params={"library_id": library_id})
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


@pytest.mark.slow
def test_unauthenticated_search_public_library(public_lib_client) -> None:
    """GET /v1/search?library_id= for a public library without auth → 200."""
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/search", params={"library_id": library_id, "q": "sunset"})
    assert r.status_code == 200


@pytest.mark.slow
def test_unauthenticated_search_private_library_returns_401(public_lib_client) -> None:
    """GET /v1/search?library_id= for a private library without auth → 401."""
    client, _, _, private_library_id = public_lib_client
    r = client.get("/v1/search", params={"library_id": private_library_id, "q": "sunset"})
    assert r.status_code == 401


@pytest.mark.slow
def test_unauthenticated_similar_public_library(public_lib_client) -> None:
    """GET /v1/similar?library_id= for a public library without auth → 404 (no asset) not 401."""
    client, _, library_id, _ = public_lib_client
    r = client.get(
        "/v1/similar",
        params={"asset_id": "ast_nonexistent", "library_id": library_id},
    )
    # Middleware resolved the tenant (public library); handler returns 404 (no asset)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Unauthenticated POST: always blocked (middleware only passes GET to public path)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_post_blocked(public_lib_client) -> None:
    """POST without auth on any library route → 401, never reaches public resolution."""
    client, _, library_id, _ = public_lib_client
    r = client.post(f"/v1/libraries/{library_id}", json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Non-eligible route: unauthenticated GET returns 401 even with valid library_id
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_non_eligible_route_blocked(public_lib_client) -> None:
    """GET /v1/libraries without auth → 401 regardless of public libraries."""
    client, _, _, _ = public_lib_client
    r = client.get("/v1/libraries")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Handler-level is_public verification: stale control plane row
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_handler_rejects_stale_public_libraries_row(public_lib_client) -> None:
    """
    Middleware resolves tenant from public_libraries even when library is_public=false in tenant DB.
    The handler's is_public check must catch this and return 404.
    """
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Make it private via PATCH (removes CP row too)
    r = client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=auth)
    assert r.status_code == 200

    # Re-insert a stale CP row manually (bypassing the route handler)
    from src.core.database import get_control_session
    from src.repository.control_plane import ApiKeyRepository, PublicLibraryRepository, TenantDbRoutingRepository
    with get_control_session() as ctrl_session:
        actual_api_key_obj = ApiKeyRepository(ctrl_session).get_by_plaintext(api_key)
        tenant_id = actual_api_key_obj.tenant_id
        routing = TenantDbRoutingRepository(ctrl_session).get_by_tenant_id(tenant_id)
        PublicLibraryRepository(ctrl_session).upsert(library_id, tenant_id, routing.connection_string)

    # Middleware now resolves tenant (CP row exists) but handler sees is_public=False → 404
    r = client.get(f"/v1/libraries/{library_id}")
    assert r.status_code == 404

    # Cleanup: restore public so other tests aren't affected
    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
