"""A search saved as a project (Robert, Oct 9).

"Projects should be explicit not implicit": a project is a list of clips.
A search is saved as a search (GET/POST /v1/views); saving it as a project
(POST /v1/projects with from_search) puts the clips it matches now in an
explicit list, which the search doesn't change afterwards. The search is
the filter algebra JSON GET /v1/query takes:
  {"filters": [{"type": "camera_make", "value": "Canon"}, ...], "sort": "taken_at", "direction": "desc"}
"""

from __future__ import annotations

import io
import json
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


def _ingest_asset(
    client,
    api_key,
    library_id,
    rel_path,
    *,
    exif_data=None,
    vision_data=None,
) -> str:
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (100, 100), color=(50, 100, 150))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    data = {
        "lineage": ingest_made(),
        "library_id": library_id,
        "rel_path": rel_path,
        "file_size": "1000",
        "media_type": "image",
        "width": "100",
        "height": "100",
    }
    if exif_data is not None:
        data["exif"] = json.dumps(exif_data)
    if vision_data is not None:
        data["vision"] = json.dumps(vision_data)

    r = client.post(
        "/v1/ingest",
        data=data,
        files={"proxy": ("proxy.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["asset_id"]


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="module")
def search_env():
    """Testcontainers env for saving searches as projects."""
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
        os.environ["ADMIN_KEY"] = "test-admin-savedsearch"
        os.environ["JWT_SECRET"] = "test-jwt-secret-savedsearch"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "SavedSearchTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-savedsearch"},
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
                r_lib = client.post(
                    "/v1/libraries",
                    json={"name": "SearchLib", "root_path": "/tmp/search-lib"},
                    headers=_headers(api_key),
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield client, api_key, library_id

        _engines.clear()


def _save_as_project(client, api_key, name, filters, **extra):
    return client.post("/v1/projects", json={"name": name, "from_search": {"filters": filters}, **extra},
                       headers=_headers(api_key))


def _clip_ids(client, api_key, project_id) -> list[str]:
    r = client.get(f"/v1/projects/{project_id}/assets", headers=_headers(api_key))
    assert r.status_code == 200, r.text
    return [i["asset_id"] for i in r.json()["items"]]


@pytest.mark.slow
def test_a_search_saved_as_a_project_holds_what_matches_now(search_env):
    client, api_key, library_id = search_env
    canon = _ingest_asset(client, api_key, library_id, "now/canon.jpg",
                          exif_data={"camera_make": "Canon", "taken_at": "2024-06-01T10:00:00+00:00"})
    _ingest_asset(client, api_key, library_id, "now/sony.jpg",
                  exif_data={"camera_make": "Sony", "taken_at": "2024-06-02T10:00:00+00:00"})
    r = _save_as_project(client, api_key, "Canon now", [{"type": "camera_make", "value": "Canon"},
                                                         {"type": "library", "value": library_id}])
    assert r.status_code == 201, r.text
    project = r.json()
    assert project["asset_count"] == 1 and "type" not in project and "saved_query" not in project
    assert _clip_ids(client, api_key, project["project_id"]) == [canon]

    # A clip that matches later isn't added: the project is an explicit list.
    _ingest_asset(client, api_key, library_id, "now/canon-later.jpg",
                  exif_data={"camera_make": "Canon", "taken_at": "2024-06-03T10:00:00+00:00"})
    assert _clip_ids(client, api_key, project["project_id"]) == [canon]


@pytest.mark.slow
def test_its_an_explicit_list_clips_are_added_removed_and_reordered(search_env):
    client, api_key, library_id = search_env
    a = _ingest_asset(client, api_key, library_id, "list/a.jpg", exif_data={"camera_make": "Leica"})
    b = _ingest_asset(client, api_key, library_id, "list/b.jpg", exif_data={"camera_make": "Fuji"})
    project_id = _save_as_project(client, api_key, "Leica", [{"type": "camera_make", "value": "Leica"},
                                                             {"type": "library", "value": library_id}]).json()["project_id"]
    h = _headers(api_key)
    assert client.post(f"/v1/projects/{project_id}/assets", json={"asset_ids": [b]}, headers=h).status_code in (200, 201)
    assert client.patch(f"/v1/projects/{project_id}/reorder", json={"asset_ids": [b, a]}, headers=h).status_code == 200
    assert _clip_ids(client, api_key, project_id) == [b, a]
    assert client.request("DELETE", f"/v1/projects/{project_id}/assets", json={"asset_ids": [a]},
                          headers=h).status_code in (200, 204)
    assert _clip_ids(client, api_key, project_id) == [b]


@pytest.mark.slow
def test_rating_filters_read_the_savers_ratings(search_env):
    client, api_key, library_id = search_env
    picked = _ingest_asset(client, api_key, library_id, "rated/picked.jpg")
    passed = _ingest_asset(client, api_key, library_id, "rated/passed.jpg")
    client.put(f"/v1/assets/{picked}/rating", json={"stars": 5, "color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{passed}/rating", json={"stars": 2}, headers=_headers(api_key))
    r = _save_as_project(client, api_key, "Top red", [{"type": "stars", "value": "4+"}, {"type": "color", "value": "red"},
                                                      {"type": "library", "value": library_id}])
    assert _clip_ids(client, api_key, r.json()["project_id"]) == [picked]


@pytest.mark.slow
def test_a_text_search_narrowed_by_filters_is_saved_from_its_candidates(search_env):
    from unittest.mock import MagicMock

    client, api_key, library_id = search_env
    canon = _ingest_asset(client, api_key, library_id, "text/canon.jpg", exif_data={"camera_make": "Canon"})
    sony = _ingest_asset(client, api_key, library_id, "text/sony.jpg", exif_data={"camera_make": "Sony"})
    qw = MagicMock()
    qw.enabled = True
    qw.search_tenant.return_value = [{"asset_id": canon, "score": 0.95}, {"asset_id": sony, "score": 0.8}]
    qw.search_tenant_scenes.return_value = []
    qw.search_tenant_transcripts.return_value = []
    with patch("src.server.search.quickwit_client.QuickwitClient", return_value=qw):
        r = _save_as_project(client, api_key, "Disney Canon", [{"type": "query", "value": "Disney"},
                                                               {"type": "camera_make", "value": "Canon"},
                                                               {"type": "library", "value": library_id}])
    assert r.status_code == 201, r.text
    assert _clip_ids(client, api_key, r.json()["project_id"]) == [canon]


@pytest.mark.slow
def test_a_search_matching_nothing_makes_an_empty_project(search_env):
    client, api_key, library_id = search_env
    r = _save_as_project(client, api_key, "Nothing", [{"type": "camera_make", "value": "Hasselblad-none"},
                                                      {"type": "library", "value": library_id}])
    assert r.status_code == 201 and r.json()["asset_count"] == 0


@pytest.mark.slow
def test_clips_or_a_search_not_both_and_not_too_many(search_env):
    client, api_key, library_id = search_env
    a = _ingest_asset(client, api_key, library_id, "many/a.jpg")
    _ingest_asset(client, api_key, library_id, "many/b.jpg")
    r = _save_as_project(client, api_key, "Both", [{"type": "library", "value": library_id}], asset_ids=[a])
    assert r.status_code == 400
    with patch("src.server.api.routers.projects._MAX_NEW_CLIPS", 1):
        r = _save_as_project(client, api_key, "Too many", [{"type": "library", "value": library_id}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "search_too_big", r.text


@pytest.mark.slow
def test_there_are_no_smart_projects_any_more(search_env):
    # An old client's "smart" project is just a project of the clips it was given (none).
    client, api_key, library_id = search_env
    r = client.post("/v1/projects", json={"name": "Old smart", "type": "smart",
                                          "saved_query": {"filters": [{"type": "library", "value": library_id}]}},
                    headers=_headers(api_key))
    assert r.status_code == 201 and r.json()["asset_count"] == 0 and "type" not in r.json()
