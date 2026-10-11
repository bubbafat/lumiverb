"""Searching in a project, or everything but it: f=project:<id> and
f=not_project:<id> on GET /v1/query, the facets and a project made from a
search (POST /v1/projects from_search).

What's in a project is its owner's business unless they share it, so each
route checks the caller can see the project (projects.guard_project_filters):
an editor (like every project route) who owns it or was shared it; 404 for
anything else, including a project in the trash or no id at all.
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
from tests.conftest import PG_IMAGE, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.machine_lineage import ingest_made

PATHS = ["harbor/a.jpg", "harbor/b.jpg", "market/c.jpg", "market/d.jpg"]


def _ingest_asset(client, api_key, library_id, rel_path) -> str:
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    r = client.post(
        "/v1/ingest",
        data={
            "lineage": ingest_made(),
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
def env():
    """Four clips; project P (the admin key's, private) holds a and c. Keys of
    their own (each its own owner): another editor and a viewer."""
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
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(u.set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-project-filter"
        os.environ["JWT_SECRET"] = "test-jwt-secret-project-filter"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.accounts.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "ProjectFilterTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-project-filter"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                tenant_id = r.json()["tenant_id"]
                admin_key = r.json()["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            from src.server.database import get_control_session
            from src.server.repository.control_plane import TenantDbRoutingRepository

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                r = client.post(
                    "/v1/libraries", json={"name": "Lib", "root_path": "/tmp/project-filter-lib"},
                    headers=_headers(admin_key),
                )
                assert r.status_code == 200, r.text
                library_id = r.json()["library_id"]
                ids = {p: _ingest_asset(client, admin_key, library_id, p) for p in PATHS}

                r = client.post(
                    "/v1/projects",
                    json={"name": "P", "asset_ids": [ids["harbor/a.jpg"], ids["market/c.jpg"]]},
                    headers=_headers(admin_key),
                )
                assert r.status_code == 201, r.text
                project_id = r.json()["project_id"]

                keys = {}
                for role in ("editor", "viewer"):
                    r = client.post("/v1/keys", json={"label": f"other-{role}", "role": role}, headers=_headers(admin_key))
                    assert r.status_code in (200, 201), r.text
                    keys[role] = r.json()["plaintext"]

                yield {
                    "client": client, "owner": admin_key, "editor": keys["editor"], "viewer": keys["viewer"],
                    "library_id": library_id, "ids": ids, "project_id": project_id,
                }

        _engines.clear()


def _query(env, key, *filters, limit=200, after=None):
    params = [("f", f) for f in filters] + [("limit", str(limit))]
    if after:
        params.append(("after", after))
    return env["client"].get("/v1/query", params=params, headers=_headers(key))


def _paths(env, response) -> set[str]:
    assert response.status_code == 200, response.text
    by_id = {v: k for k, v in env["ids"].items()}
    return {by_id[item["asset_id"]] for item in response.json()["items"]}


@pytest.mark.slow
def test_in_project_finds_only_its_clips(env):
    r = _query(env, env["owner"], f"project:{env['project_id']}")
    assert _paths(env, r) == {"harbor/a.jpg", "market/c.jpg"}


@pytest.mark.slow
def test_not_in_project_finds_everything_else(env):
    r = _query(env, env["owner"], f"not_project:{env['project_id']}")
    assert _paths(env, r) == {"harbor/b.jpg", "market/d.jpg"}


@pytest.mark.slow
def test_with_a_text_search(env):
    """A text search's candidates are filtered in Postgres like any other filter."""
    pid = env["project_id"]
    assert _paths(env, _query(env, env["owner"], "query:harbor", f"project:{pid}")) == {"harbor/a.jpg"}
    assert _paths(env, _query(env, env["owner"], "query:harbor", f"not_project:{pid}")) == {"harbor/b.jpg"}


@pytest.mark.slow
def test_pages_hold_exactly_the_clips_outside_the_project(env):
    """The server filters before paging: full pages, none skipped or repeated."""
    pid = env["project_id"]
    seen: list[str] = []
    after = None
    for _ in range(5):
        r = _query(env, env["owner"], f"not_project:{pid}", limit=1, after=after)
        assert r.status_code == 200, r.text
        seen += [item["asset_id"] for item in r.json()["items"]]
        after = r.json()["next_cursor"]
        if not after:
            break
    by_id = {v: k for k, v in env["ids"].items()}
    assert sorted(by_id[i] for i in seen) == ["harbor/b.jpg", "market/d.jpg"]


@pytest.mark.slow
def test_another_users_private_project_is_not_found_until_shared(env):
    client, pid = env["client"], env["project_id"]
    for f in (f"project:{pid}", f"not_project:{pid}"):
        r = _query(env, env["editor"], f)
        assert r.status_code == 404, (f, r.status_code, r.text)

    r = client.patch(f"/v1/projects/{pid}", json={"visibility": "shared"}, headers=_headers(env["owner"]))
    assert r.status_code == 200, r.text
    try:
        assert _paths(env, _query(env, env["editor"], f"project:{pid}")) == {"harbor/a.jpg", "market/c.jpg"}
    finally:
        r = client.patch(f"/v1/projects/{pid}", json={"visibility": "private"}, headers=_headers(env["owner"]))
        assert r.status_code == 200, r.text


@pytest.mark.slow
def test_viewers_cant_filter_by_project(env):
    """Every project route is an editor's."""
    r = _query(env, env["viewer"], f"project:{env['project_id']}")
    assert r.status_code == 403, r.text


@pytest.mark.slow
@pytest.mark.parametrize("prefix", ["project", "not_project"])
def test_no_id_or_an_unknown_one_is_not_found(env, prefix):
    """Never dropped: an ignored not_project filter would search everything."""
    for value in ("", "prj_nope"):
        r = _query(env, env["owner"], f"{prefix}:{value}")
        assert r.status_code == 404, (value, r.status_code, r.text)


@pytest.mark.slow
def test_a_project_in_the_trash_is_not_found(env):
    client = env["client"]
    r = client.post("/v1/projects", json={"name": "Trashed", "asset_ids": [env["ids"]["harbor/b.jpg"]]},
                    headers=_headers(env["owner"]))
    assert r.status_code == 201, r.text
    trashed = r.json()["project_id"]
    assert client.delete(f"/v1/projects/{trashed}", headers=_headers(env["owner"])).status_code == 204
    assert _query(env, env["owner"], f"project:{trashed}").status_code == 404


@pytest.mark.slow
def test_facets_check_the_project_too(env):
    client, pid = env["client"], env["project_id"]
    r = client.get("/v1/assets/facets", params=[("f", f"project:{pid}")], headers=_headers(env["owner"]))
    assert r.status_code == 200, r.text
    assert r.json()["media_types"] == ["image"]
    r = client.get("/v1/assets/facets", params=[("f", f"project:{pid}")], headers=_headers(env["editor"]))
    assert r.status_code == 404, r.text


@pytest.mark.slow
def test_a_project_made_from_a_search_checks_the_project(env):
    client, pid = env["client"], env["project_id"]
    search = {"filters": [{"type": "not_project", "value": pid}]}

    r = client.post("/v1/projects", json={"name": "Peek", "from_search": search}, headers=_headers(env["editor"]))
    assert r.status_code == 404, r.text

    r = client.post("/v1/projects", json={"name": "Rest", "from_search": search}, headers=_headers(env["owner"]))
    assert r.status_code == 201, r.text
    rest = r.json()["project_id"]
    assert _paths(env, _query(env, env["owner"], f"project:{rest}")) == {"harbor/b.jpg", "market/d.jpg"}
