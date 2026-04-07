"""Libraries API tests. TestClient + testcontainers Postgres; create tenant via admin API with provision_tenant_database mocked, then provision tenant DB manually."""

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


@pytest.fixture(scope="module")
def libraries_client() -> tuple[TestClient, str]:
    """
    Two testcontainers Postgres: one for control plane, one for tenant. Create a real tenant
    via the admin API (with provision_tenant_database mocked), then point routing at the
    second container and run tenant migrations there. Returns (client, api_key).
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
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "LibrariesTestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        # Second container for tenant DB; run tenant migrations, then point routing at it
        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = tenant_postgres.get_connection_url()
            tenant_url = _ensure_psycopg2(tenant_url)
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
                yield client, api_key

        _engines.clear()


@pytest.mark.slow
def test_create_library(libraries_client: tuple[TestClient, str]) -> None:
    """POST /v1/libraries returns 200 and library_id starts with lib_."""
    client, api_key = libraries_client
    r = client.post(
        "/v1/libraries",
        json={"name": "My Photos", "root_path": "/photos"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["library_id"].startswith("lib_")
    assert data["name"] == "My Photos"
    assert data["root_path"] == "/photos"


@pytest.mark.slow
def test_create_library_duplicate_name(libraries_client: tuple[TestClient, str]) -> None:
    """POST same name twice: second request returns 409."""
    client, api_key = libraries_client
    name = "UniqueName_" + __import__("secrets").token_urlsafe(8)
    r1 = client.post(
        "/v1/libraries",
        json={"name": name, "root_path": "/path1"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/v1/libraries",
        json={"name": name, "root_path": "/path2"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r2.status_code == 409


@pytest.mark.slow
def test_list_libraries(libraries_client: tuple[TestClient, str]) -> None:
    """Create two libraries, GET /v1/libraries returns both."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    client.post("/v1/libraries", json={"name": "ListA", "root_path": "/a"}, headers=auth)
    client.post("/v1/libraries", json={"name": "ListB", "root_path": "/b"}, headers=auth)
    r = client.get("/v1/libraries", headers=auth)
    assert r.status_code == 200
    libraries = r.json()
    names = {lib["name"] for lib in libraries}
    assert "ListA" in names
    assert "ListB" in names
    for lib in libraries:
        assert "library_id" in lib
        assert lib["library_id"].startswith("lib_")
        assert "name" in lib
        assert "root_path" in lib
        assert "last_scan_at" in lib


@pytest.mark.slow
def test_create_library_requires_auth(libraries_client: tuple[TestClient, str]) -> None:
    """POST /v1/libraries without Authorization header returns 401."""
    client, _ = libraries_client
    r = client.post(
        "/v1/libraries",
        json={"name": "NoAuth", "root_path": "/nope"},
    )
    assert r.status_code == 401


@pytest.mark.slow
def test_delete_library_soft_deletes(libraries_client: tuple[TestClient, str]) -> None:
    """Create library, call DELETE /v1/libraries/{library_id}; assert 204, then GET without include_trashed excludes it, GET with include_trashed=true includes it with status=trashed."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post(
        "/v1/libraries",
        json={"name": "ToTrash", "root_path": "/trash"},
        headers=auth,
    )
    assert r.status_code == 200
    library_id = r.json()["library_id"]

    r_del = client.delete(f"/v1/libraries/{library_id}", headers=auth)
    assert r_del.status_code == 204

    r_list = client.get("/v1/libraries", headers=auth)
    assert r_list.status_code == 200
    libraries = r_list.json()
    assert not any(lib["library_id"] == library_id for lib in libraries)

    r_list_all = client.get("/v1/libraries", params={"include_trashed": True}, headers=auth)
    assert r_list_all.status_code == 200
    libraries_all = r_list_all.json()
    trashed = [lib for lib in libraries_all if lib["library_id"] == library_id]
    assert len(trashed) == 1
    assert trashed[0]["status"] == "trashed"


@pytest.mark.slow
def test_empty_trash_hard_deletes(libraries_client: tuple[TestClient, str]) -> None:
    """Create library, trash it, call POST /v1/libraries/empty-trash; assert response deleted=1 and library gone from DB."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/libraries",
        json={"name": "HardDelLib", "root_path": "/hard"},
        headers=auth,
    )
    assert r.status_code == 200
    library_id = r.json()["library_id"]

    client.delete(f"/v1/libraries/{library_id}", headers=auth)

    r_empty = client.post("/v1/libraries/empty-trash", headers=auth)
    assert r_empty.status_code == 200
    deleted = r_empty.json()["deleted"]
    assert deleted >= 1, "empty-trash should have deleted at least our library"

    r_list = client.get("/v1/libraries", params={"include_trashed": True}, headers=auth)
    assert r_list.status_code == 200
    assert not any(lib["library_id"] == library_id for lib in r_list.json()), "our library should be gone"


@pytest.mark.slow
def test_delete_already_trashed_returns_409(libraries_client: tuple[TestClient, str]) -> None:
    """Trash a library, then try DELETE again; assert 409."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}

    r = client.post(
        "/v1/libraries",
        json={"name": "AlreadyTrashed", "root_path": "/x"},
        headers=auth,
    )
    assert r.status_code == 200
    library_id = r.json()["library_id"]

    client.delete(f"/v1/libraries/{library_id}", headers=auth)
    r_again = client.delete(f"/v1/libraries/{library_id}", headers=auth)
    assert r_again.status_code == 409


# ---------------------------------------------------------------------------
# is_public / public_libraries invariant tests
# ---------------------------------------------------------------------------

def _get_public_libraries_row(library_id: str):
    """Return the public_libraries control plane row for library_id, or None."""
    from src.server.database import get_control_session
    from src.server.models.control_plane import PublicLibrary
    with get_control_session() as session:
        return session.get(PublicLibrary, library_id)


@pytest.mark.slow
def test_patch_is_public_true_inserts_control_plane_row(libraries_client: tuple[TestClient, str]) -> None:
    """PATCH is_public=true: response has is_public=true and a row exists in public_libraries."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "PubLib_" + __import__("secrets").token_urlsafe(6), "root_path": "/pub"}, headers=auth)
    assert r.status_code == 200
    library_id = r.json()["library_id"]
    assert r.json()["is_public"] is False

    r_patch = client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
    assert r_patch.status_code == 200
    assert r_patch.json()["is_public"] is True

    row = _get_public_libraries_row(library_id)
    assert row is not None, "public_libraries row should exist after setting is_public=true"


@pytest.mark.slow
def test_patch_is_public_false_removes_control_plane_row(libraries_client: tuple[TestClient, str]) -> None:
    """PATCH is_public=false: row removed from public_libraries."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "PubLib2_" + __import__("secrets").token_urlsafe(6), "root_path": "/pub2"}, headers=auth)
    library_id = r.json()["library_id"]

    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
    assert _get_public_libraries_row(library_id) is not None

    r_patch = client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=auth)
    assert r_patch.status_code == 200
    assert r_patch.json()["is_public"] is False
    assert _get_public_libraries_row(library_id) is None


@pytest.mark.slow
def test_patch_toggle_invariant(libraries_client: tuple[TestClient, str]) -> None:
    """Toggle is_public back and forth; control plane row matches each time."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "Toggle_" + __import__("secrets").token_urlsafe(6), "root_path": "/tog"}, headers=auth)
    library_id = r.json()["library_id"]

    for is_public in [True, False, True, False]:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": is_public}, headers=auth)
        row = _get_public_libraries_row(library_id)
        if is_public:
            assert row is not None
        else:
            assert row is None


@pytest.mark.slow
def test_patch_no_is_public_does_not_touch_control_plane(libraries_client: tuple[TestClient, str]) -> None:
    """PATCH with only name (no is_public) leaves public_libraries unchanged."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "NoIsPub_" + __import__("secrets").token_urlsafe(6), "root_path": "/nip"}, headers=auth)
    library_id = r.json()["library_id"]

    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
    assert _get_public_libraries_row(library_id) is not None

    client.patch(f"/v1/libraries/{library_id}", json={"name": "RenamedLib"}, headers=auth)
    assert _get_public_libraries_row(library_id) is not None, "name-only PATCH must not remove public_libraries row"


@pytest.mark.slow
def test_trash_public_library_removes_control_plane_row(libraries_client: tuple[TestClient, str]) -> None:
    """DELETE (trash) a public library removes its public_libraries row."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "TrashPub_" + __import__("secrets").token_urlsafe(6), "root_path": "/tp"}, headers=auth)
    library_id = r.json()["library_id"]

    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
    assert _get_public_libraries_row(library_id) is not None

    r_del = client.delete(f"/v1/libraries/{library_id}", headers=auth)
    assert r_del.status_code == 204
    assert _get_public_libraries_row(library_id) is None


@pytest.mark.slow
def test_hard_delete_public_library_removes_control_plane_row(libraries_client: tuple[TestClient, str]) -> None:
    """empty-trash on a public library cleans up the public_libraries row."""
    client, api_key = libraries_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "HardPub_" + __import__("secrets").token_urlsafe(6), "root_path": "/hp"}, headers=auth)
    library_id = r.json()["library_id"]

    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)
    assert _get_public_libraries_row(library_id) is not None

    # Trash the library WITHOUT using the DELETE endpoint so the CP row is not
    # removed by the trash handler — exercise the empty-trash cleanup path directly.
    from src.server.database import get_control_session
    from src.server.repository.tenant import LibraryRepository as TenantLibraryRepo
    from src.server.database import get_engine_for_url
    from sqlmodel import Session as SqlSession
    with get_control_session() as ctrl_session:
        from src.server.repository.control_plane import TenantDbRoutingRepository
        routing = TenantDbRoutingRepository(ctrl_session).get_by_tenant_id(
            _get_public_libraries_row(library_id).tenant_id
        )
        tenant_url = routing.connection_string
    engine = get_engine_for_url(tenant_url)
    with SqlSession(engine) as tsession:
        TenantLibraryRepo(tsession).trash(library_id)

    # CP row still present (trash via repo bypassed the route handler)
    assert _get_public_libraries_row(library_id) is not None

    r_empty = client.post("/v1/libraries/empty-trash", headers=auth)
    assert r_empty.status_code == 200
    assert _get_public_libraries_row(library_id) is None
