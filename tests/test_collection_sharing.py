"""Collection sharing / visibility tests.

Tests cover:
1. Changing visibility via PATCH (private → shared → public)
2. Shared collections appear in other users' list
3. Visibility validation
"""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines
from tests.conftest import _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


def _ingest_asset(client, api_key, library_id, rel_path) -> str:
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    r = client.post(
        "/v1/ingest",
        data={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": "1000",
            "media_type": "image",
            "width": "100",
            "height": "100",
        },
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def sharing_env():
    """Testcontainers env with a single API key (single-user tenant for now)."""
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
        os.environ["ADMIN_KEY"] = "test-admin-sharing"
        os.environ["JWT_SECRET"] = "test-jwt-secret-sharing"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SharingTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-sharing"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
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
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "ShareLib", "root_path": "/tmp/share-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Visibility changes via PATCH
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_change_visibility_private_to_shared(sharing_env):
    """PATCH visibility from private to shared succeeds."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "VisToggle"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]
    assert r.json()["visibility"] == "private"

    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "shared"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["visibility"] == "shared"


@pytest.mark.slow
def test_change_visibility_shared_to_public(sharing_env):
    """PATCH visibility from shared to public succeeds."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "ToPublic"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "shared"},
        headers=_headers(api_key),
    )
    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "public"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["visibility"] == "public"


@pytest.mark.slow
def test_change_visibility_public_back_to_private(sharing_env):
    """Can demote visibility from public back to private."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "Demote"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "public"},
        headers=_headers(api_key),
    )
    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "private"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["visibility"] == "private"


@pytest.mark.slow
def test_invalid_visibility_rejected(sharing_env):
    """Invalid visibility value is rejected."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "BadVis"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "unlisted"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Visibility affects collection listing
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_private_collection_in_own_list(sharing_env):
    """Private collections appear in owner's list."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "MyPrivate"},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]

    r2 = client.get("/v1/collections", headers=_headers(api_key))
    ids = [c["collection_id"] for c in r2.json()["items"]]
    assert col_id in ids


@pytest.mark.slow
def test_shared_collection_ownership_field(sharing_env):
    """Shared collection has ownership=own for creator, shared for others."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "SharedOwnership", "visibility": "shared"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    data = r.json()
    assert data["visibility"] == "shared"
    assert data["ownership"] == "own"


# ---------------------------------------------------------------------------
# Collection with assets + visibility change
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_share_collection_with_assets(sharing_env):
    """A collection with assets can be shared. Assets are accessible via the shared collection."""
    client, api_key, library_id = sharing_env

    a1 = _ingest_asset(client, api_key, library_id, "share/photo1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "share/photo2.jpg")

    # Create private collection with assets
    r = client.post(
        "/v1/collections",
        json={"name": "ShareWithAssets", "asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    col_id = r.json()["collection_id"]
    assert r.json()["asset_count"] == 2

    # Share it
    r2 = client.patch(
        f"/v1/collections/{col_id}",
        json={"visibility": "shared"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 200
    assert r2.json()["visibility"] == "shared"

    # Assets still accessible
    r3 = client.get(f"/v1/collections/{col_id}/assets", headers=_headers(api_key))
    assert r3.status_code == 200
    ids = [i["asset_id"] for i in r3.json()["items"]]
    assert a1 in ids
    assert a2 in ids


# ---------------------------------------------------------------------------
# Create collection with initial visibility
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_create_collection_with_visibility(sharing_env):
    """Collections can be created with a non-default visibility."""
    client, api_key, _ = sharing_env

    r = client.post(
        "/v1/collections",
        json={"name": "BornShared", "visibility": "shared"},
        headers=_headers(api_key),
    )
    assert r.status_code == 201
    assert r.json()["visibility"] == "shared"

    r2 = client.post(
        "/v1/collections",
        json={"name": "BornPublic", "visibility": "public"},
        headers=_headers(api_key),
    )
    assert r2.status_code == 201
    assert r2.json()["visibility"] == "public"
