"""Slow tests for the /v1/search endpoint using Postgres fallback."""

import os
import secrets
from typing import Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def search_client() -> Tuple[_AuthClient, str, str]:
    """
    Two Postgres containers; create tenant; point routing at tenant DB.
    Returns (_AuthClient, library_id) for issuing authenticated requests.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
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
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SearchApiTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
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
                auth_client = _AuthClient(client, api_key)

                # Create a library for search tests.
                lib_name = "SearchLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/search"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _insert_asset_with_metadata(tenant_url: str, library_id: str, description: str, tag: str) -> str:
    """Insert an asset and moondream metadata row directly into tenant DB."""
    engine = create_engine(tenant_url)
    asset_id = "ast_" + secrets.token_urlsafe(8)
    with engine.begin() as conn:
        import json

        rel_path = f"photos/{secrets.token_urlsafe(4)}.jpg"
        conn.execute(
            text(
                """
                INSERT INTO assets (
                    asset_id, library_id, rel_path, file_size, file_mtime,
                    media_type, availability, status, created_at, updated_at
                )
                VALUES (
                    :asset_id, :library_id, :rel_path, 123, NOW(),
                    'image/jpeg', 'online', 'pending', NOW(), NOW()
                )
                """
            ),
            {
                "asset_id": asset_id,
                "library_id": library_id,
                "rel_path": rel_path,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO asset_metadata (
                    metadata_id, asset_id, model_id, model_version, generated_at, data
                )
                VALUES (
                    :metadata_id, :asset_id, 'moondream', '2', NOW(),
                    :data
                )
                """
            ),
            {
                "metadata_id": "meta_" + secrets.token_urlsafe(8),
                "asset_id": asset_id,
                "data": json.dumps({"description": description, "tags": [tag]}),
            },
        )
    engine.dispose()
    return asset_id


@pytest.mark.slow
def test_search_postgres_fallback(search_client: Tuple[_AuthClient, str, str]) -> None:
    """With quickwit_enabled=False, /v1/search should return Postgres ILIKE results."""
    auth_client, library_id, tenant_url = search_client

    # Force Quickwit disabled and allow fallback.
    os.environ["QUICKWIT_ENABLED"] = "false"
    os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
    get_settings.cache_clear()

    description = "A golden sunset over the mountains."
    _insert_asset_with_metadata(tenant_url, library_id, description, "sunset")

    r = auth_client.get(
        "/v1/search",
        params={"library_id": library_id, "q": "sunset"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert data["source"] == "postgres"
    assert data["total"] >= 1
    assert any("sunset" in hit["description"] for hit in data["hits"])


@pytest.mark.slow
def test_search_no_results(search_client: Tuple[_AuthClient, str, str]) -> None:
    """Searching for a non-existent term returns empty hits."""
    auth_client, library_id, _tenant_url = search_client

    os.environ["QUICKWIT_ENABLED"] = "false"
    os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
    get_settings.cache_clear()

    r = auth_client.get(
        "/v1/search",
        params={"library_id": library_id, "q": "term_that_does_not_exist_12345"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert data["total"] == 0
    assert data["hits"] == []


@pytest.mark.slow
def test_search_quickwit_fallback_on_error(search_client: Tuple[_AuthClient, str, str]) -> None:
    """
    When quickwit_enabled=True but QuickwitClient.search raises,
    the endpoint should fall back to Postgres results if fallback is enabled.
    """
    auth_client, library_id, tenant_url = search_client

    os.environ["QUICKWIT_ENABLED"] = "true"
    os.environ["QUICKWIT_FALLBACK_TO_POSTGRES"] = "true"
    get_settings.cache_clear()

    description = "A quiet forest path at dawn."
    _insert_asset_with_metadata(tenant_url, library_id, description, "forest")

    # Make QuickwitClient.search raise to trigger fallback path.
    with patch("src.search.quickwit_client.QuickwitClient.search") as mock_search:
        mock_search.side_effect = ConnectionError("Quickwit unavailable")

        r = auth_client.get(
            "/v1/search",
            params={"library_id": library_id, "q": "forest"},
        )

    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert data["source"] == "postgres"
    assert data["total"] >= 1
    assert any("forest" in hit["description"] for hit in data["hits"])

