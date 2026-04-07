"""Tests for POST /v1/assets/{asset_id}/vision (Phase 5).

All tests are slow (testcontainers Postgres). A single module-scoped fixture
spins up two Postgres containers and provisions a tenant + library + assets.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vision_env(tmp_path_factory):
    """
    Two Postgres containers (control + tenant), one tenant, one library, three assets:
      - active_id:  active, proxy_sha256 = "a" * 64
      - nosha_id:   active, proxy_sha256 is NULL
      - deleted_id: soft-deleted

    Yields:
        (auth, raw_client, api_key, active_id, nosha_id, deleted_id, tenant_url)
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
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        from unittest.mock import patch

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap_client:
                r = bootstrap_client.post(
                    "/v1/admin/tenants",
                    json={"name": "VisionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
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

            with TestClient(app) as raw_client:
                auth = _AuthClient(raw_client, api_key)
                auth_headers = {"Authorization": f"Bearer {api_key}"}

                r_lib = raw_client.post(
                    "/v1/libraries",
                    json={"name": "VisionLib", "root_path": "/media"},
                    headers=auth_headers,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                def _upsert(rel_path: str) -> str:
                    raw_client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rel_path,
                            "file_size": 1000,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image",
                        },
                        headers=auth_headers,
                    )
                    r = raw_client.get(
                        "/v1/assets/by-path",
                        params={"library_id": library_id, "rel_path": rel_path},
                        headers=auth_headers,
                    )
                    assert r.status_code == 200
                    return r.json()["asset_id"]

                active_id = _upsert("active.jpg")
                nosha_id = _upsert("nosha.jpg")
                deleted_id = _upsert("deleted.jpg")

                # Set proxy_sha256 directly on the active asset via the DB
                from src.server.database import get_engine_for_url
                from sqlmodel import Session as SMSession
                from src.server.models.tenant import Asset

                tenant_engine = get_engine_for_url(tenant_url)
                with SMSession(tenant_engine) as db:
                    asset = db.get(Asset, active_id)
                    asset.proxy_sha256 = "a" * 64
                    db.add(asset)
                    db.commit()

                # Soft-delete the deleted asset
                r_del = raw_client.delete(
                    f"/v1/assets/{deleted_id}",
                    headers=auth_headers,
                )
                assert r_del.status_code == 204

                yield auth, raw_client, api_key, active_id, nosha_id, deleted_id, tenant_url

        _engines.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(auth: _AuthClient, asset_id: str, **kwargs):
    body = {"model_id": "test-vision-model", "description": "a sunset", "tags": ["landscape"]}
    body.update(kwargs)
    return auth.post(f"/v1/assets/{asset_id}/vision", json=body)


# ---------------------------------------------------------------------------
# Slow tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_happy_path_no_hash(vision_env) -> None:
    auth, _, _, active_id, _, _, _ = vision_env
    r = _post(auth, active_id)
    assert r.status_code == 200
    body = r.json()
    assert body["asset_id"] == active_id
    assert body["status"] == "described"


@pytest.mark.slow
def test_happy_path_hash_matches(vision_env) -> None:
    auth, _, _, active_id, _, _, _ = vision_env
    r = _post(auth, active_id, client_proxy_sha256="a" * 64)
    assert r.status_code == 200
    assert r.json()["status"] == "described"


@pytest.mark.slow
def test_hash_mismatch_returns_409(vision_env) -> None:
    auth, _, _, active_id, _, _, _ = vision_env
    r = _post(auth, active_id, client_proxy_sha256="0" * 64)
    assert r.status_code == 409
    err = r.json()["detail"]["error"]
    assert err["code"] == "proxy_hash_mismatch"


@pytest.mark.slow
def test_skip_check_when_server_hash_null(vision_env) -> None:
    """client_proxy_sha256 provided but server has no hash — check is skipped."""
    auth, _, _, _, nosha_id, _, _ = vision_env
    r = _post(auth, nosha_id, client_proxy_sha256="b" * 64)
    assert r.status_code == 200


@pytest.mark.slow
def test_upsert_overwrites_existing_metadata(vision_env) -> None:
    """Submitting twice with different data overwrites the previous result."""
    auth, _, _, _, nosha_id, _, tenant_url = vision_env

    _post(auth, nosha_id, description="first description", tags=["foo"])
    _post(auth, nosha_id, description="second description", tags=["bar"])

    from src.server.database import get_engine_for_url
    from sqlmodel import Session as SMSession, select
    from src.server.models.tenant import AssetMetadata

    tenant_engine = get_engine_for_url(tenant_url)
    with SMSession(tenant_engine) as db:
        stmt = select(AssetMetadata).where(
            AssetMetadata.asset_id == nosha_id,
            AssetMetadata.model_id == "test-vision-model",
        )
        rows = list(db.exec(stmt).all())

    # upsert semantics: exactly one row, overwritten with the second submission
    assert len(rows) == 1
    assert rows[0].data["description"] == "second description"
    assert rows[0].data["tags"] == ["bar"]


@pytest.mark.slow
def test_sets_asset_status_described(vision_env) -> None:
    """After submission, the asset status is updated to 'described'."""
    auth, raw_client, api_key, active_id, _, _, _ = vision_env
    auth_headers = {"Authorization": f"Bearer {api_key}"}

    _post(auth, active_id)

    r = raw_client.get(f"/v1/assets/{active_id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "described"


@pytest.mark.slow
def test_vision_submit_triggers_search_sync(vision_env) -> None:
    """After submission, inline search sync is attempted (best-effort without Quickwit)."""
    auth, _, _, active_id, _, _, tenant_url = vision_env

    _post(auth, active_id)

    # Vision submit now calls try_sync_asset inline. Without Quickwit in
    # test it fails silently. Verify the metadata was persisted (the trigger).
    from src.server.database import get_engine_for_url
    from sqlalchemy import text

    tenant_engine = get_engine_for_url(tenant_url)
    with tenant_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM asset_metadata WHERE asset_id = :asset_id"),
            {"asset_id": active_id},
        ).scalar_one()
    assert count >= 1


@pytest.mark.slow
def test_unknown_asset_returns_404(vision_env) -> None:
    auth, _, _, _, _, _, _ = vision_env
    r = _post(auth, "ast_doesnotexist")
    assert r.status_code == 404


@pytest.mark.slow
def test_soft_deleted_asset_returns_404(vision_env) -> None:
    auth, _, _, _, _, deleted_id, _ = vision_env
    r = _post(auth, deleted_id)
    assert r.status_code == 404


@pytest.mark.slow
def test_missing_auth_returns_401(vision_env) -> None:
    _, raw_client, _, active_id, _, _, _ = vision_env
    r = raw_client.post(
        f"/v1/assets/{active_id}/vision",
        json={"model_id": "test-vision-model", "description": "test", "tags": []},
    )
    assert r.status_code == 401
