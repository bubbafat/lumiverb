"""Tests for POST /v1/assets/state-check (Phase 4).

All tests are slow (testcontainers Postgres). A single module-scoped fixture spins
up two Postgres containers and provisions a tenant + library + assets.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.api.main import app
from src.core.config import get_settings
from src.core.database import _engines
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def state_check_env(tmp_path_factory):
    """
    Two Postgres containers (control + tenant), one tenant, one library, three assets:
      - active_id:  active, has proxy_sha256 set
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

        with patch("src.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap_client:
                r = bootstrap_client.post(
                    "/v1/admin/tenants",
                    json={"name": "StateCheckTenant", "plan": "free"},
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
                    json={"name": "StateLib", "root_path": "/media"},
                    headers=auth_headers,
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                r_scan = raw_client.post(
                    "/v1/scans",
                    json={"library_id": library_id, "status": "running"},
                    headers=auth_headers,
                )
                assert r_scan.status_code == 200
                scan_id = r_scan.json()["scan_id"]

                def _upsert(rel_path: str) -> str:
                    raw_client.post(
                        "/v1/assets/upsert",
                        json={
                            "library_id": library_id,
                            "rel_path": rel_path,
                            "file_size": 1000,
                            "file_mtime": "2025-01-01T12:00:00Z",
                            "media_type": "image/jpeg",
                            "scan_id": scan_id,
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
                from src.core.database import get_engine_for_url
                from sqlmodel import Session as SMSession
                from src.models.tenant import Asset

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


def _post(auth: _AuthClient, asset_ids: list[str]):
    return auth.post("/v1/assets/state-check", json={"asset_ids": asset_ids})


# ---------------------------------------------------------------------------
# Slow tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_active_asset_not_deleted(state_check_env) -> None:
    auth, _, _, active_id, _, _, _ = state_check_env
    r = _post(auth, [active_id])
    assert r.status_code == 200
    item = r.json()["assets"][0]
    assert item["asset_id"] == active_id
    assert item["deleted"] is False
    assert item["proxy_sha256"] == "a" * 64


@pytest.mark.slow
def test_soft_deleted_asset_shows_deleted_true(state_check_env) -> None:
    auth, _, _, _, _, deleted_id, _ = state_check_env
    r = _post(auth, [deleted_id])
    assert r.status_code == 200
    item = r.json()["assets"][0]
    assert item["asset_id"] == deleted_id
    assert item["deleted"] is True


@pytest.mark.slow
def test_unknown_asset_treated_as_deleted(state_check_env) -> None:
    auth, _, _, _, _, _, _ = state_check_env
    r = _post(auth, ["ast_doesnotexist"])
    assert r.status_code == 200
    item = r.json()["assets"][0]
    assert item["asset_id"] == "ast_doesnotexist"
    assert item["deleted"] is True
    assert item["proxy_sha256"] is None


@pytest.mark.slow
def test_null_proxy_sha256(state_check_env) -> None:
    auth, _, _, _, nosha_id, _, _ = state_check_env
    r = _post(auth, [nosha_id])
    assert r.status_code == 200
    item = r.json()["assets"][0]
    assert item["deleted"] is False
    assert item["proxy_sha256"] is None


@pytest.mark.slow
def test_mixed_batch(state_check_env) -> None:
    auth, _, _, active_id, nosha_id, deleted_id, _ = state_check_env
    unknown_id = "ast_unknown999"
    ids = [active_id, deleted_id, unknown_id, nosha_id]
    r = _post(auth, ids)
    assert r.status_code == 200
    assets = {item["asset_id"]: item for item in r.json()["assets"]}

    assert assets[active_id]["deleted"] is False
    assert assets[active_id]["proxy_sha256"] == "a" * 64

    assert assets[deleted_id]["deleted"] is True

    assert assets[unknown_id]["deleted"] is True
    assert assets[unknown_id]["proxy_sha256"] is None

    assert assets[nosha_id]["deleted"] is False
    assert assets[nosha_id]["proxy_sha256"] is None


@pytest.mark.slow
def test_response_order_matches_input(state_check_env) -> None:
    auth, _, _, active_id, nosha_id, deleted_id, _ = state_check_env
    ids = [nosha_id, active_id, deleted_id]
    r = _post(auth, ids)
    assert r.status_code == 200
    returned_ids = [item["asset_id"] for item in r.json()["assets"]]
    assert returned_ids == ids


@pytest.mark.slow
def test_exactly_500_ids_accepted(state_check_env) -> None:
    auth, _, _, active_id, _, _, _ = state_check_env
    ids = [active_id] + [f"ast_fake{i:04d}" for i in range(499)]
    r = _post(auth, ids)
    assert r.status_code == 200
    assert len(r.json()["assets"]) == 500


@pytest.mark.slow
def test_501_ids_rejected(state_check_env) -> None:
    auth, _, _, _, _, _, _ = state_check_env
    ids = [f"ast_fake{i:04d}" for i in range(501)]
    r = _post(auth, ids)
    assert r.status_code == 422


@pytest.mark.slow
def test_empty_list_rejected(state_check_env) -> None:
    auth, _, _, _, _, _, _ = state_check_env
    r = _post(auth, [])
    assert r.status_code == 422


@pytest.mark.slow
def test_missing_auth_returns_401(state_check_env) -> None:
    _, raw_client, _, active_id, _, _, _ = state_check_env
    r = raw_client.post("/v1/assets/state-check", json={"asset_ids": [active_id]})
    assert r.status_code == 401


@pytest.mark.slow
def test_tenant_isolation(state_check_env, tmp_path_factory) -> None:
    """Asset IDs from another tenant are treated as not found (deleted=True)."""
    auth, _, _, active_id, _, _, tenant_url = state_check_env

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Save original env so we can restore it after this test
    orig_ctrl_url = os.environ["CONTROL_PLANE_DATABASE_URL"]
    orig_tenant_tpl = os.environ["TENANT_DATABASE_URL_TEMPLATE"]

    try:
        with PostgresContainer("pgvector/pgvector:pg16") as ctrl_pg2:
            ctrl_url2 = _ensure_psycopg2(ctrl_pg2.get_connection_url())
            engine2 = create_engine(ctrl_url2)
            with engine2.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
            engine2.dispose()
            _run_control_migrations(ctrl_url2)

            u2 = make_url(ctrl_url2)
            tenant_tpl2 = str(u2.set(database="{tenant_id}"))
            os.environ["CONTROL_PLANE_DATABASE_URL"] = ctrl_url2
            os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl2
            get_settings.cache_clear()
            _engines.clear()

            from unittest.mock import patch

            with patch("src.api.routers.admin.provision_tenant_database"):
                with TestClient(app) as bc2:
                    r2 = bc2.post(
                        "/v1/admin/tenants",
                        json={"name": "OtherTenant", "plan": "free"},
                        headers={"Authorization": "Bearer test-admin-secret"},
                    )
                    assert r2.status_code == 200
                    api_key2 = r2.json()["api_key"]
                    tenant_id2 = r2.json()["tenant_id"]

            with PostgresContainer("pgvector/pgvector:pg16") as tp2:
                tenant_url2 = _ensure_psycopg2(tp2.get_connection_url())
                _provision_tenant_db(tenant_url2, project_root)

                from src.core.database import get_control_session
                from src.repository.control_plane import TenantDbRoutingRepository

                with get_control_session() as sess:
                    repo = TenantDbRoutingRepository(sess)
                    row = repo.get_by_tenant_id(tenant_id2)
                    assert row is not None
                    row.connection_string = tenant_url2
                    sess.add(row)
                    sess.commit()

                with TestClient(app) as rc2:
                    auth2 = _AuthClient(rc2, api_key2)
                    # active_id belongs to the first tenant — other tenant sees it as not found
                    r = auth2.post("/v1/assets/state-check", json={"asset_ids": [active_id]})
                    assert r.status_code == 200
                    item = r.json()["assets"][0]
                    assert item["deleted"] is True
                    assert item["proxy_sha256"] is None
    finally:
        # Restore original env so subsequent tests in this module still work
        os.environ["CONTROL_PLANE_DATABASE_URL"] = orig_ctrl_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = orig_tenant_tpl
        get_settings.cache_clear()
        _engines.clear()


@pytest.mark.slow
def test_route_not_matched_as_asset_id(state_check_env) -> None:
    """POST /v1/assets/state-check must not be matched as asset_id='state-check'."""
    auth, _, _, _, _, _, _ = state_check_env
    r = _post(auth, ["ast_fake"])
    # Must return 200 (state-check endpoint), not 404/405 from an asset handler
    assert r.status_code == 200
