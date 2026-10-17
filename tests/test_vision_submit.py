"""A clip's description saved as the vision producer saves it (POST
/v1/assets/batch-vision, one item: tests/derived.py), and the per-clip route
it replaced answering 410.

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
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import made
from tests.derived import saved


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vision_env(tmp_path_factory):
    """
    Two Postgres containers (control + tenant), one tenant, one library, three assets:
      - active_id, nosha_id: active
      - deleted_id: soft-deleted

    Yields:
        (auth, raw_client, api_key, active_id, nosha_id, deleted_id, tenant_url)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_pg:
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

        with patch("src.server.accounts.provision_tenant_database"):
            with TestClient(app) as bootstrap_client:
                r = bootstrap_client.post(
                    "/v1/admin/tenants",
                    json={"name": "VisionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
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

                active_id = make_clip(tenant_url, library_id, "active.jpg")
                nosha_id = make_clip(tenant_url, library_id, "nosha.jpg")
                deleted_id = make_clip(tenant_url, library_id, "deleted.jpg")

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
    body = {"model_id": "test-vision-model", "description": "a sunset", "tags": ["landscape"],
            "lineage": made("vision")}
    body.update(kwargs)
    return saved(auth, "vision", asset_id, json=body)


# ---------------------------------------------------------------------------
# Slow tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_happy_path(vision_env) -> None:
    auth, _, _, active_id, _, _, _ = vision_env
    r = _post(auth, active_id)
    assert r.status_code == 200
    assert r.json() == {"updated": 1, "skipped": 0}


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["vision", "ocr", "embeddings"])
def test_the_per_clip_route_is_gone_naming_the_batch_one(vision_env, kind) -> None:
    auth, _, _, active_id, _, _, _ = vision_env
    r = auth.post(f"/v1/assets/{active_id}/{kind}", json={"description": "a sunset", "lineage": made("vision")})
    assert r.status_code == 410, r.text
    error = r.json()["error"]
    assert error["code"] == "gone" and error["details"]["replaced_by"] == [f"POST /v1/assets/batch-{kind}"]


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
    """After submission, the clip's search row is written at once (ADR-019) and marked synced."""
    auth, _, _, active_id, _, _, tenant_url = vision_env

    _post(auth, active_id)

    from sqlalchemy import text

    from src.server.database import get_engine_for_url

    tenant_engine = get_engine_for_url(tenant_url)
    with tenant_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM asset_metadata WHERE asset_id = :asset_id"),
            {"asset_id": active_id},
        ).scalar_one()
        row = conn.execute(text("SELECT described FROM asset_search WHERE asset_id = :a"), {"a": active_id}).first()
        synced = conn.execute(text("SELECT search_synced_at FROM assets WHERE asset_id = :a"),
                              {"a": active_id}).scalar_one()
    assert count >= 1
    assert row is not None and row.described, "its words are searchable at once"
    assert synced is not None


@pytest.mark.slow
def test_a_clip_that_isnt_there_or_is_in_the_trash_is_skipped(vision_env) -> None:
    auth, _, _, _, _, deleted_id, tenant_url = vision_env
    for clip in ("ast_doesnotexist", deleted_id):
        r = _post(auth, clip)
        assert r.status_code == 200 and r.json() == {"updated": 0, "skipped": 1}
    from src.server.database import get_engine_for_url

    with get_engine_for_url(tenant_url).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM asset_metadata WHERE asset_id = :a"),
                            {"a": deleted_id}).scalar_one() == 0, "nothing saved"


@pytest.mark.slow
def test_missing_auth_returns_401(vision_env) -> None:
    _, raw_client, _, active_id, _, _, _ = vision_env
    r = saved(raw_client, "vision", active_id, json={"model_id": "test-vision-model", "description": "test", "tags": []},
    )
    assert r.status_code == 401
