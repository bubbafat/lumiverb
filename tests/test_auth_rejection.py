"""Auth rejection tests: all major endpoints reject requests without valid Authorization."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines

from tests.conftest import _ensure_psycopg2, _run_control_migrations


@pytest.fixture(scope="module")
def auth_rejection_client() -> TestClient:
    """
    Single control-plane Postgres; no tenant. App can resolve auth; requests without
    valid Authorization get 401. Used to test that all major endpoints reject no-auth.
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

        with TestClient(app) as client:
            yield client

        _engines.clear()


@pytest.mark.slow
def test_assets_list_no_auth(auth_rejection_client: TestClient) -> None:
    """GET /v1/assets without auth returns 401."""
    r = auth_rejection_client.get("/v1/assets", params={"library_id": "lib_foo"})
    assert r.status_code == 401


@pytest.mark.slow
def test_assets_get_no_auth(auth_rejection_client: TestClient) -> None:
    """GET /v1/assets/{id} without auth returns 401."""
    r = auth_rejection_client.get("/v1/assets/ast_00000000000000000000000000")
    assert r.status_code == 401


@pytest.mark.slow
def test_video_scenes_no_auth(auth_rejection_client: TestClient) -> None:
    """GET /v1/video/{asset_id}/scenes without auth returns 401."""
    r = auth_rejection_client.get("/v1/video/ast_00000000000000000000000000/scenes")
    assert r.status_code == 401


@pytest.mark.slow
def test_search_no_auth(auth_rejection_client: TestClient) -> None:
    """GET /v1/search without auth returns 401."""
    r = auth_rejection_client.get(
        "/v1/search",
        params={"library_id": "lib_foo", "q": "sunset"},
    )
    assert r.status_code == 401


@pytest.mark.slow
def test_similarity_no_auth(auth_rejection_client: TestClient) -> None:
    """GET /v1/similar without auth returns 401."""
    r = auth_rejection_client.get(
        "/v1/similar",
        params={
            "asset_id": "ast_00000000000000000000000000",
            "library_id": "lib_foo",
        },
    )
    assert r.status_code == 401
