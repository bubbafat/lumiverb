"""Tests for tenant-level key management API and CLI."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer
from typer.testing import CliRunner

from src.api.main import app
from src.cli.main import app as cli_app
from src.core.config import get_settings
from src.core.database import _engines

from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


runner = CliRunner()


@pytest.fixture(scope="module")
def keys_client() -> tuple[TestClient, str]:
    """
    Control-plane + tenant DB; create one tenant and default key.
    Returns (client, api_key) for tenant-auth tests.
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

        # Create tenant + default key via admin API (mock provision_tenant_database).
        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "KeysTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        # Provision tenant DB and wire routing.
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
                yield client, api_key

        _engines.clear()


@pytest.mark.slow
def test_non_admin_cannot_create_or_revoke_keys(keys_client: tuple[TestClient, str]) -> None:
    """Non-admin key calling POST /v1/keys or DELETE /v1/keys/{id} returns 403."""
    client, api_key = keys_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Default key is admin; create a non-admin key first.
    r_admin = client.post(
        "/v1/keys",
        json={"label": "non-admin", "role": "member"},
        headers=auth,
    )
    assert r_admin.status_code == 200
    non_admin_id = r_admin.json()["key_id"]

    # Use non-admin key for subsequent calls.
    auth_non_admin = {"Authorization": f"Bearer {r_admin.json()['plaintext']}"}

    r_create = client.post(
        "/v1/keys",
        json={"label": "should-fail", "role": "member"},
        headers=auth_non_admin,
    )
    assert r_create.status_code == 403

    r_delete = client.delete(f"/v1/keys/{non_admin_id}", headers=auth_non_admin)
    assert r_delete.status_code == 403


@pytest.mark.slow
def test_self_revoke_returns_409(keys_client: tuple[TestClient, str]) -> None:
    """DELETE /v1/keys/{own_key_id} returns 409."""
    client, api_key = keys_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r_list = client.get("/v1/keys", headers=auth)
    assert r_list.status_code == 200
    keys = r_list.json()["keys"]
    assert keys, "expected at least one key"
    own_id = keys[0]["key_id"]

    r = client.delete(f"/v1/keys/{own_id}", headers=auth)
    assert r.status_code == 409


@pytest.mark.slow
def test_last_admin_key_cannot_be_revoked(keys_client: tuple[TestClient, str]) -> None:
    """Attempting to revoke the only admin key (as a member key) returns 409 with code last_admin_key."""
    client, admin_plaintext = keys_client
    auth_admin = {"Authorization": f"Bearer {admin_plaintext}"}

    # Create a member key; we will use it to try to revoke the admin key.
    r_create = client.post(
        "/v1/keys",
        json={"label": "member", "role": "member"},
        headers=auth_admin,
    )
    assert r_create.status_code == 200
    member_plaintext = r_create.json()["plaintext"]
    auth_member = {"Authorization": f"Bearer {member_plaintext}"}

    # Ensure there is exactly one admin key.
    r_list = client.get("/v1/keys", headers=auth_member)
    assert r_list.status_code == 200
    keys = r_list.json()["keys"]
    admin_keys = [k for k in keys if k.get("role") == "admin"]
    assert len(admin_keys) == 1
    admin_id = admin_keys[0]["key_id"]

    # Member key tries to revoke the only admin key → last_admin_key.
    r = client.delete(f"/v1/keys/{admin_id}", headers=auth_member)
    assert r.status_code == 409
    body = r.json()
    assert body.get("error", {}).get("code") == "last_admin_key"


@pytest.mark.slow
def test_non_admin_key_can_be_revoked_when_single_admin_exists(keys_client: tuple[TestClient, str]) -> None:
    """Revoking a non-admin key succeeds even when there is only one admin key."""
    client, api_key = keys_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Create a non-admin key.
    r_create = client.post(
        "/v1/keys",
        json={"label": "temp", "role": "member"},
        headers=auth,
    )
    assert r_create.status_code == 200
    non_admin_id = r_create.json()["key_id"]

    # There should still be exactly one admin key.
    r_list = client.get("/v1/keys", headers=auth)
    keys = r_list.json()["keys"]
    admin_keys = [k for k in keys if k.get("role") == "admin"]
    assert len(admin_keys) == 1

    # Revoking the non-admin key should succeed.
    r_delete = client.delete(f"/v1/keys/{non_admin_id}", headers=auth)
    assert r_delete.status_code == 204


@pytest.mark.fast
def test_cli_keys_list_uses_api() -> None:
    """lumi keys list calls GET /v1/keys and renders table."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "keys": [
            {
                "key_id": "key_01A",
                "label": "default",
                "role": "admin",
                "last_used_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
    }
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.commands.keys.LumiverbClient", return_value=mock_client):
        result = runner.invoke(cli_app, ["keys", "list"])

    assert result.exit_code == 0
    assert "key_01A" in result.output
    assert "default" in result.output
    mock_client.get.assert_called_once_with("/v1/keys")


@pytest.mark.fast
def test_cli_keys_create_prints_plaintext() -> None:
    """lumi keys create prints plaintext and table row."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "key_id": "key_01A",
        "label": "ci-read-only",
        "role": "member",
        "plaintext": "lv_01ABC",
        "created_at": "2026-01-01T00:00:00Z",
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("src.cli.commands.keys.LumiverbClient", return_value=mock_client):
        result = runner.invoke(cli_app, ["keys", "create", "--label", "ci-read-only"])

    assert result.exit_code == 0
    assert "lv_01ABC" in result.output
    assert "ci-read-only" in result.output
    mock_client.post.assert_called_once()


@pytest.mark.fast
def test_cli_keys_revoke_confirms_and_handles_204() -> None:
    """lumi keys revoke prompts for confirmation and prints success on 204."""
    from httpx import Response, Request

    mock_client = MagicMock()

    req = Request("DELETE", "http://example.com")
    resp = Response(204, request=req)
    mock_client.raw.return_value = resp

    with patch("src.cli.commands.keys.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            cli_app,
            ["keys", "revoke", "--key-id", "key_01A"],
            input="y\n",
        )

    assert result.exit_code == 0
    assert "Revoked key_01A" in result.output

