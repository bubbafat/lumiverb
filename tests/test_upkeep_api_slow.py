"""Upkeep endpoints: who may call them, and on what.

An account admin's API key (what `lumiverb maintenance` sends) acts on that
account; the operator's admin key acts on every account only when it says
tenants=all (what the systemd timers send). Upkeep routes skip the tenant
middleware, so the router resolves the tenant from the key itself.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """Yields (client, tenant_api_key, tenant_id, data_dir)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tmp_path_factory.mktemp("upkeep_data")

    with PostgresContainer(PG_IMAGE) as control_pg:
        control_url = _ensure_psycopg2(control_pg.get_connection_url())
        _run_control_migrations(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(
            make_url(control_url).set(database="{tenant_id}")
        )
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["DATA_DIR"] = str(data_dir)
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as bootstrap:
                r = bootstrap.post(
                    "/v1/admin/tenants",
                    json={"name": "UpkeepTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, r.text
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_pg:
            tenant_url = _ensure_psycopg2(tenant_pg.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)
            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                yield client, api_key, tenant_id, data_dir

    os.environ.pop("DATA_DIR", None)
    get_settings.cache_clear()
    _engines.clear()


@pytest.mark.slow
def test_cleanup_with_tenant_key_runs_dry_run(env) -> None:
    client, api_key, *_ = env

    r = client.post(
        "/v1/upkeep/cleanup", params={"dry_run": "true", "all": "true"},
        headers={"Authorization": f"Bearer {api_key}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["orphan_files"] == 0


@pytest.mark.slow
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-key"}])
def test_cleanup_without_valid_key_is_401(env, headers) -> None:
    client, *_ = env

    r = client.post("/v1/upkeep/cleanup", headers=headers)

    assert r.status_code == 401, r.text


def _new_key(client, api_key: str, role: str) -> tuple[str, str]:
    r = client.post(
        "/v1/keys", json={"label": f"{role}-key", "role": role},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["key_id"], r.json()["plaintext"]


@pytest.mark.slow
@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_cleanup_needs_an_admin_key(env, role) -> None:
    """Cleanup deletes files: a viewer or editor key can't run it."""
    client, api_key, *_ = env
    _, key = _new_key(client, api_key, role)

    r = client.post(
        "/v1/upkeep/cleanup", params={"dry_run": "false"},
        headers={"Authorization": f"Bearer {key}"},
    )

    assert r.status_code == 403, r.text


@pytest.mark.slow
def test_cleanup_with_revoked_key_is_401(env) -> None:
    client, api_key, *_ = env
    key_id, key = _new_key(client, api_key, "admin")
    r = client.delete(f"/v1/keys/{key_id}", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code in (200, 204), r.text

    r = client.post("/v1/upkeep/cleanup", headers={"Authorization": f"Bearer {key}"})

    assert r.status_code == 401, r.text


ADMIN = {"Authorization": "Bearer test-admin-secret"}
ENDPOINTS = ["/v1/upkeep", "/v1/upkeep/search-sync", "/v1/upkeep/cleanup", "/v1/upkeep/recluster",
             "/v1/upkeep/recreate-search-indexes", "/v1/upkeep/cleanup-dismissed"]


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-key"}, {"Authorization": "Basic x"}])
def test_every_upkeep_call_without_a_live_key_is_401(env, path, headers) -> None:
    client, *_ = env

    r = client.post(path, headers=headers)

    assert r.status_code == 401, r.text


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_every_upkeep_call_needs_an_account_admin(env, path, role) -> None:
    """A viewer's key can't wipe the search indexes or force a reindex (nor anything else here)."""
    client, api_key, *_ = env
    _, key = _new_key(client, api_key, role)

    r = client.post(path, params={"force": "true"}, headers={"Authorization": f"Bearer {key}"})

    assert r.status_code == 403, r.text


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
def test_an_account_admin_acts_on_the_account(env, path) -> None:
    client, api_key, *_ = env
    params = {"all": "true"} if path == "/v1/upkeep/cleanup" else {}

    r = client.post(path, params=params, headers={"Authorization": f"Bearer {api_key}"})

    assert r.status_code == 200, r.text


@pytest.mark.slow
@pytest.mark.parametrize("params", [{}, {"dry_run": "false"}, {"library_id": "lib_x", "all": "true"}])
def test_cleanup_with_an_account_key_names_a_library_or_all(env, params) -> None:
    """Nothing cleans the whole account from a missing argument (Robert)."""
    client, api_key, *_ = env

    r = client.post("/v1/upkeep/cleanup", params=params, headers={"Authorization": f"Bearer {api_key}"})

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "scope_required"


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
def test_an_account_key_cant_ask_for_every_account(env, path) -> None:
    client, api_key, *_ = env

    r = client.post(path, params={"tenants": "all"}, headers={"Authorization": f"Bearer {api_key}"})

    assert r.status_code == 403, r.text


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
def test_the_admin_key_must_name_every_account(env, path) -> None:
    """Nothing acts on every account from a missing argument (Robert)."""
    client, *_ = env

    r = client.post(path, headers=ADMIN)

    assert r.status_code == 400, r.text
    assert "tenants=all" in r.text


@pytest.mark.slow
@pytest.mark.parametrize("path", ENDPOINTS)
def test_the_admin_key_with_tenants_all_acts_on_every_account(env, path) -> None:
    client, *_ = env

    r = client.post(path, params={"tenants": "all"}, headers=ADMIN)

    assert r.status_code == 200, r.text


@pytest.mark.slow
def test_tenants_takes_only_all(env) -> None:
    client, *_ = env

    r = client.post("/v1/upkeep", params={"tenants": "ten_someone"}, headers=ADMIN)

    assert r.status_code == 422, r.text


@pytest.mark.slow
def test_upkeep_for_every_account_reaches_the_account(env) -> None:
    client, *_ = env

    r = client.post("/v1/upkeep/cleanup", params={"tenants": "all"}, headers=ADMIN)

    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True


def _old(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (1, 1))


@pytest.mark.slow
def test_cleanup_of_one_library_leaves_the_rest_of_the_account(env) -> None:
    client, api_key, tenant_id, data_dir = env
    headers = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "CleanOne", "root_path": "/tmp/clean-one"}, headers=headers)
    assert r.status_code in (200, 201), r.text
    library_id = r.json()["library_id"]
    _old(data_dir / tenant_id / library_id / "proxies" / "00" / "stray.webp")
    _old(data_dir / tenant_id / "lib_gone" / "proxies" / "00" / "stray.webp")

    one = client.post("/v1/upkeep/cleanup", params={"library_id": library_id}, headers=headers)
    whole = client.post("/v1/upkeep/cleanup", params={"all": "true"}, headers=headers)

    assert one.status_code == 200, one.text
    # The library's one stray file is all of it: held back by the 25% guard, but looked at.
    assert (one.json()["orphan_folders"], one.json()["skipped_libraries"]) == ([], 1)
    assert [f["folder_id"] for f in whole.json()["orphan_folders"]] == ["lib_gone"]
    assert whole.json()["skipped_libraries"] == 1
    assert (data_dir / tenant_id / "lib_gone").exists()  # dry runs both


@pytest.mark.slow
def test_cleanup_never_removes_a_folder_the_db_doesnt_list(env) -> None:
    """A database restored from an older backup misses newer libraries: their
    folders are reported, and only an admin naming them removes them (Robert)."""
    client, api_key, tenant_id, data_dir = env
    headers = {"Authorization": f"Bearer {api_key}"}
    _old(data_dir / tenant_id / "lib_newer" / "proxies" / "00" / "p.webp")

    r = client.post("/v1/upkeep/cleanup", params={"all": "true", "dry_run": "false"}, headers=headers)

    assert r.status_code == 200, r.text
    [folder] = [f for f in r.json()["orphan_folders"] if f["folder_id"] == "lib_newer"]
    assert folder["path"] == f"{tenant_id}/lib_newer" and folder["bytes"] == 1 and folder["newest_mtime"]
    assert (data_dir / tenant_id / "lib_newer").exists()


@pytest.mark.slow
def test_removing_orphan_folders_needs_ids_or_all_and_an_admin(env) -> None:
    client, api_key, *_ = env
    headers = {"Authorization": f"Bearer {api_key}"}
    for body in ({}, {"folder_ids": ["lib_x"], "all": True}):
        r = client.post("/v1/upkeep/remove-orphan-folders", json=body, headers=headers)
        assert r.status_code == 400 and r.json()["error"]["code"] == "scope_required", r.text
    _, key = _new_key(client, api_key, "editor")
    r = client.post("/v1/upkeep/remove-orphan-folders", json={"all": True}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403, r.text


@pytest.mark.slow
def test_removing_orphan_folders_takes_only_unlisted_ones(env) -> None:
    client, api_key, tenant_id, data_dir = env
    headers = {"Authorization": f"Bearer {api_key}"}
    r = client.post("/v1/libraries", json={"name": "Listed", "root_path": "/tmp/listed"}, headers=headers)
    listed = r.json()["library_id"]
    _old(data_dir / tenant_id / listed / "proxies" / "00" / "p.webp")
    _old(data_dir / tenant_id / "lib_orphan" / "proxies" / "00" / "p.webp")
    body = {"folder_ids": ["lib_orphan", listed]}

    dry = client.post("/v1/upkeep/remove-orphan-folders", json={**body, "dry_run": True}, headers=headers)
    assert dry.status_code == 200, dry.text
    assert [f["folder_id"] for f in dry.json()["removed"]] == ["lib_orphan"]
    assert (data_dir / tenant_id / "lib_orphan").exists()

    r = client.post("/v1/upkeep/remove-orphan-folders", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert [f["folder_id"] for f in r.json()["removed"]] == ["lib_orphan"] and r.json()["not_found"] == [listed]
    assert not (data_dir / tenant_id / "lib_orphan").exists()
    assert (data_dir / tenant_id / listed).exists()

    r = client.post("/v1/upkeep/remove-orphan-folders", params={"tenants": "all"}, json={"all": True, "dry_run": True},
                    headers=ADMIN)
    assert r.status_code == 200, r.text


@pytest.mark.slow
def test_cleanup_of_a_library_not_the_accounts_is_404(env) -> None:
    client, api_key, *_ = env

    r = client.post("/v1/upkeep/cleanup", params={"library_id": "lib_nope"},
                    headers={"Authorization": f"Bearer {api_key}"})

    assert r.status_code == 404, r.text


@pytest.mark.slow
def test_cleanup_of_a_library_needs_an_account_key(env) -> None:
    client, *_ = env

    r = client.post("/v1/upkeep/cleanup", params={"tenants": "all", "library_id": "lib_x"}, headers=ADMIN)
    assert r.status_code == 400, r.text
    r = client.post("/v1/upkeep/cleanup", params={"tenants": "all", "all": "true"}, headers=ADMIN)
    assert r.status_code == 400, r.text


@pytest.mark.slow
def test_cleanup_dismissed_works_with_the_accounts_admin_key(env) -> None:
    """What `lumiverb maintenance cleanup-dismissed` and repair's redetect-faces send."""
    client, api_key, *_ = env

    r = client.post("/v1/upkeep/cleanup-dismissed", headers={"Authorization": f"Bearer {api_key}"})

    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 0}
