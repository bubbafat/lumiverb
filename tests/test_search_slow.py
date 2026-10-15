"""Slow tests for text search through GET /v1/query: PostgreSQL's search in the account's database (ADR-019)."""

import os
import secrets
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.clips import make_clip
from tests.conftest import (
    PG_IMAGE,
    _AuthClient,
    _ensure_psycopg2,
    _provision_tenant_db,
    _run_control_migrations,
)
from tests.machine_lineage import made


@pytest.fixture(scope="module")
def search_client() -> tuple[_AuthClient, str, str]:
    """
    Two Postgres containers; create tenant; point routing at tenant DB.
    Returns (_AuthClient, library_id) for issuing authenticated requests.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_postgres:
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

        with patch("src.server.accounts.provision_tenant_database"), TestClient(app) as client:
            r = client.post(
                "/v1/admin/tenants",
                json={"name": "SearchApiTenant", "plan": "free"},
                headers={"Authorization": "Bearer test-admin-secret"},
            )
            assert r.status_code == 200, (r.status_code, r.text)
            data = r.json()
            tenant_id = data["tenant_id"]
            api_key = data["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

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


def _described(auth_client: _AuthClient, tenant_url: str, library_id: str, description: str, tag: str,
               model_id: str = "test-vision-model") -> str:
    """A clip described as the vision producer describes one (its search row is written then)."""
    asset_id = make_clip(tenant_url, library_id, f"photos/{secrets.token_hex(4)}.jpg")
    _describe(auth_client, asset_id, description, tag, model_id)
    return asset_id


def _describe(auth_client: _AuthClient, asset_id: str, description: str, tag: str, model_id: str) -> None:
    r = auth_client.post(f"/v1/assets/{asset_id}/vision", json={
        "model_id": model_id, "model_version": "2", "description": description, "tags": [tag],
        "lineage": made("vision")})
    assert r.status_code == 200, r.text


def _found(auth_client: _AuthClient, library_id: str, word: str) -> dict:
    r = auth_client.get("/v1/query", params={"f": [f"library:{library_id}", f"query:{word}"]})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def _insert_asset_with_metadata(tenant_url: str, library_id: str, description: str, tag: str) -> str:
    """Insert an asset and metadata row directly into tenant DB: written
    outside the API (a restore, a migration), so nothing syncs its search row."""
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
                    'image', 'online', 'pending', NOW(), NOW()
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
                    :metadata_id, :asset_id, 'test-vision-model', '2', NOW(),
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
def test_search_finds_a_description(search_client: tuple[_AuthClient, str, str]) -> None:
    """A described clip is found by its description's words, at once."""
    auth_client, library_id, tenant_url = search_client

    description = "A golden sunset over the mountains."
    asset_id = _described(auth_client, tenant_url, library_id, description, "sunset")

    data = _found(auth_client, library_id, "sunset")
    assert data["search_source"] == "postgres"
    assert data["total_estimate"] >= 1
    assert asset_id in {hit["asset_id"] for hit in data["items"]}


@pytest.mark.slow
def test_search_finds_any_models_latest_description(search_client: tuple[_AuthClient, str, str]) -> None:
    """Whichever model described a clip (not only one named in the code),
    its latest description is what's searched."""
    auth_client, library_id, tenant_url = search_client

    asset_id = _described(auth_client, tenant_url, library_id, "A dragonfly on a lily pad in the pond.", "insect",
                          model_id="qwen3-visioncaption-2b")
    assert asset_id in {hit["asset_id"] for hit in _found(auth_client, library_id, "dragonfly lily pad")["items"]}

    _describe(auth_client, asset_id, "A heron wading at the water's edge.", "bird", "moondream")
    assert asset_id in {hit["asset_id"] for hit in _found(auth_client, library_id, "heron")["items"]}
    assert asset_id not in {hit["asset_id"] for hit in _found(auth_client, library_id, "dragonfly")["items"]}


@pytest.mark.slow
def test_search_no_results(search_client: tuple[_AuthClient, str, str]) -> None:
    """Searching for a non-existent term returns empty hits."""
    auth_client, library_id, _tenant_url = search_client

    r = auth_client.get(
        "/v1/query",
        params={"f": [f"library:{library_id}", "query:term_that_does_not_exist_12345"]},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert data["items"] == []


@pytest.mark.slow
def test_search_finds_a_description_written_outside_the_api_once_the_sweep_runs(
        search_client: tuple[_AuthClient, str, str]) -> None:
    """A description no request saved (nothing wrote its search row) is
    found once the upkeep's search sync has run: the sweep catches up what
    the inline syncs missed."""
    auth_client, library_id, tenant_url = search_client

    description = "A quiet forest path at dawn."
    asset_id = _insert_asset_with_metadata(tenant_url, library_id, description, "forest")
    assert asset_id not in {hit["asset_id"] for hit in _found(auth_client, library_id, "forest")["items"]}

    r = auth_client.post("/v1/upkeep/search-sync")
    assert r.status_code == 200 and r.json()["synced"] >= 1 and r.json()["failed"] == 0, r.text

    data = _found(auth_client, library_id, "forest")
    assert data["search_source"] == "postgres"
    assert data["total_estimate"] >= 1
    assert asset_id in {hit["asset_id"] for hit in data["items"]}

