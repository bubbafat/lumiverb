"""Phase 3 middleware tests: public library resolution without auth."""

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


@pytest.fixture(scope="module")
def public_lib_client():
    """
    Two-container setup: control plane + tenant DB.
    Creates a tenant via admin API, provisions tenant DB, creates a library, and
    makes it public. Returns (client, api_key, library_id).
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
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(u.set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["JWT_SECRET"] = "test-jwt-secret-public-lib-mw"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "PubMiddlewareTenant", "plan": "free"},
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
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            auth = {"Authorization": f"Bearer {api_key}"}

            with TestClient(app) as client:
                # Create a public library
                r = client.post(
                    "/v1/libraries",
                    json={"name": "PublicLib", "root_path": "/pub"},
                    headers=auth,
                )
                assert r.status_code == 200, r.text
                library_id = r.json()["library_id"]

                r = client.patch(
                    f"/v1/libraries/{library_id}",
                    json={"is_public": True},
                    headers=auth,
                )
                assert r.status_code == 200, r.text

                # Create a private library for negative tests
                r = client.post(
                    "/v1/libraries",
                    json={"name": "PrivateLib", "root_path": "/priv"},
                    headers=auth,
                )
                assert r.status_code == 200, r.text
                private_library_id = r.json()["library_id"]

                yield client, api_key, library_id, private_library_id

        _engines.clear()


# ---------------------------------------------------------------------------
# Authenticated path: still works, is_public_request=False
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_authenticated_request_resolves_tenant(public_lib_client) -> None:
    """Authenticated GET /v1/libraries returns 200."""
    client, api_key, library_id, _ = public_lib_client
    r = client.get("/v1/libraries", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unauthenticated GET: public library via path param
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_get_public_library_returns_200(public_lib_client) -> None:
    """GET /v1/libraries/{id} for a public library without auth → 200."""
    client, _, library_id, _ = public_lib_client
    r = client.get(f"/v1/libraries/{library_id}")
    assert r.status_code == 200
    assert r.json()["is_public"] is True


@pytest.mark.slow
def test_unauthenticated_get_private_library_returns_401(public_lib_client) -> None:
    """GET /v1/libraries/{id} for a private library without auth → 401 from middleware."""
    client, _, _, private_library_id = public_lib_client
    r = client.get(f"/v1/libraries/{private_library_id}")
    assert r.status_code == 401


@pytest.mark.slow
def test_unauthenticated_get_directories_public_library(public_lib_client) -> None:
    """GET /v1/libraries/{id}/directories for a public library without auth → 200."""
    client, _, library_id, _ = public_lib_client
    r = client.get(f"/v1/libraries/{library_id}/directories")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Unauthenticated GET: public library via library_id query param
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_assets_page_public_library(public_lib_client) -> None:
    """GET /v1/assets/page?library_id= for a public library without auth → 200 (empty items)."""
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/page", params={"library_id": library_id})
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


@pytest.mark.slow
def test_unauthenticated_query_public_library(public_lib_client) -> None:
    """
    GET /v1/query?f=library:<id> for a public library without auth: what a
    public library's page browses with. (It 401'd from April to October
    2026, so the web's public library pages showed nothing.) The handler's
    guard keeps the request to that one public library.
    """
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/query", params=[("f", f"library:{library_id}")])
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["items"], list)


@pytest.mark.slow
@pytest.mark.parametrize("private_filter", ["favorite:true", "stars:3+", "person:per_nope"])
def test_public_queries_cant_use_signed_in_filters(public_lib_client, private_filter) -> None:
    """Ratings belong to signed-in people, and who's in a photo isn't for visitors to probe."""
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", private_filter)])
    assert r.status_code == 403, (r.status_code, r.text)


@pytest.mark.slow
def test_unauthenticated_similar_public_library(public_lib_client) -> None:
    """GET /v1/similar?library_id= for a public library without auth → 404 (no asset) not 401."""
    client, _, library_id, _ = public_lib_client
    r = client.get(
        "/v1/similar",
        params={"asset_id": "ast_nonexistent", "library_id": library_id},
    )
    # Middleware resolved the tenant (public library); handler returns 404 (no asset)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Unauthenticated POST: always blocked (middleware only passes GET to public path)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_post_blocked(public_lib_client) -> None:
    """POST without auth on any library route → 401, never reaches public resolution."""
    client, _, library_id, _ = public_lib_client
    r = client.post(f"/v1/libraries/{library_id}", json={})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Non-eligible route: unauthenticated GET returns 401 even with valid library_id
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_unauthenticated_non_eligible_route_blocked(public_lib_client) -> None:
    """GET /v1/libraries without auth → 401 regardless of public libraries."""
    client, _, _, _ = public_lib_client
    r = client.get("/v1/libraries")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Handler-level is_public verification: stale control plane row
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_handler_rejects_stale_public_libraries_row(public_lib_client) -> None:
    """
    Middleware resolves tenant from public_libraries even when library is_public=false in tenant DB.
    The handler's is_public check must catch this and return 404.
    """
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}

    # Make it private via PATCH (removes CP row too)
    r = client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=auth)
    assert r.status_code == 200

    # Re-insert a stale CP row manually (bypassing the route handler)
    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository, PublicLibraryRepository, TenantDbRoutingRepository
    with get_control_session() as ctrl_session:
        actual_api_key_obj = ApiKeyRepository(ctrl_session).get_by_plaintext(api_key)
        tenant_id = actual_api_key_obj.tenant_id
        routing = TenantDbRoutingRepository(ctrl_session).get_by_tenant_id(tenant_id)
        PublicLibraryRepository(ctrl_session).upsert(library_id, tenant_id, routing.connection_string)

    # Middleware now resolves tenant (CP row exists) but handler sees is_public=False → 404
    r = client.get(f"/v1/libraries/{library_id}")
    assert r.status_code == 404

    # Cleanup: restore public so other tests aren't affected
    client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=auth)


# ---------------------------------------------------------------------------
# /v1/query: cross-library leak prevention via public request
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_query_public_request_cannot_leak_private_library(public_lib_client) -> None:
    """
    Attack: an unauthenticated user passes both a public and a private
    library in the f=library: filter. The middleware authorizes based on
    the FIRST library: param, but without a handler-side guard the query
    would also return content from the private library.

    The endpoint must reject this — either via the get_current_user_id
    dependency (current defense) or via the explicit is_public_request
    guard in the query handler (defense in depth). Either way, the
    response must NOT be a 200 with private content.
    """
    client, _, public_library_id, private_library_id = public_lib_client
    r = client.get(
        "/v1/query",
        params=[
            ("f", f"library:{public_library_id}"),
            ("f", f"library:{private_library_id}"),
        ],
    )
    # The handler's guard: a 200 with items from the private library would be a leak.
    assert r.status_code == 404, (r.status_code, r.text)


@pytest.mark.slow
def test_query_public_request_only_private_library_returns_401(public_lib_client) -> None:
    """
    Public request with only a private library in f=library: must be
    rejected by the middleware (no authorized public library found).
    """
    client, _, _, private_library_id = public_lib_client
    r = client.get(
        "/v1/query",
        params=[("f", f"library:{private_library_id}")],
    )
    # Middleware finds the f=library:private_id, looks up public_libraries,
    # finds nothing, falls through to 401.
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# What a visitor can learn: only what the public page itself shows
# ---------------------------------------------------------------------------


def _ingest(client, api_key, library_id, rel_path, media_type="video") -> str:
    import io
    import json as _json

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 18)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post(
        "/v1/ingest",
        data={"library_id": library_id, "rel_path": rel_path, "file_size": "10", "media_type": media_type,
              "width": "32", "height": "18", "exif": _json.dumps({"sha256": os.urandom(32).hex()}),
              "lineage": ingest_made()},
        files={"proxy": ("p.jpg", buf, "image/jpeg")},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


_SRT = ("1\n00:00:02,000 --> 00:00:04,000\nhello there\n\n"
        "2\n00:01:00,000 --> 00:01:03,000\nthe zebra password\n")


@pytest.fixture(scope="module")
def spoken(public_lib_client):
    """A public video whose transcript says 'zebra' at one minute, past the 10 s public cap."""
    client, api_key, library_id, _ = public_lib_client
    asset_id = _ingest(client, api_key, library_id, "talk/clip 1.mov")
    r = client.post(f"/v1/assets/{asset_id}/transcript", json={"source": "manual", "srt": _SRT, "language": "en"},
                    headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200, r.text
    return asset_id


@pytest.mark.slow
def test_public_search_cant_reach_words_past_the_cap(public_lib_client, spoken):
    client, api_key, library_id, _ = public_lib_client
    params = [("f", f"library:{library_id}"), ("f", "query:zebra")]
    public = client.get("/v1/query", params=params)
    assert public.status_code == 200, public.text
    assert [i["asset_id"] for i in public.json()["items"]] == []
    signed_in = client.get("/v1/query", params=params, headers={"Authorization": f"Bearer {api_key}"})
    assert spoken in [i["asset_id"] for i in signed_in.json()["items"]]
    # Lifted, public pages show the whole transcript, so search may find it.
    auth = {"Authorization": f"Bearer {api_key}"}
    client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": None}, headers=auth)
    try:
        assert spoken in [i["asset_id"] for i in client.get("/v1/query", params=params).json()["items"]]
    finally:
        client.patch("/v1/tenant/settings", json={"public_video_preview_max_seconds": 10}, headers=auth)


@pytest.mark.slow
def test_public_facets_stay_in_the_public_library(public_lib_client):
    client, _, library_id, private_library_id = public_lib_client
    assert client.get("/v1/assets/facets", params=[("f", f"library:{library_id}")]).status_code == 200
    # No library scope: the aggregation would cover the whole account.
    assert client.get("/v1/assets/facets", params={"library_id": library_id}).status_code == 403
    both = [("f", f"library:{library_id}"), ("f", f"library:{private_library_id}")]
    assert client.get("/v1/assets/facets", params=both).status_code == 404
    for private_filter in ("favorite:true", "stars:3+", "person:per_nope"):
        r = client.get("/v1/assets/facets", params=[("f", f"library:{library_id}"), ("f", private_filter)])
        assert r.status_code == 403, (private_filter, r.status_code)


@pytest.mark.slow
def test_public_facet_search_cant_reach_words_past_the_cap(public_lib_client, spoken):
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/facets", params=[("f", f"library:{library_id}"), ("f", "query:zebra")])
    assert r.status_code == 200, r.text
    assert r.json()["media_types"] == []


@pytest.mark.slow
@pytest.mark.parametrize("path", ["/v1/assets/{asset}/faces", "/v1/libraries/{library}/ignored-paths",
                                  "/v1/assets/repair-summary?library_id={library}", "/v1/system/health"])
def test_signed_in_only_routes_refuse_visitors(public_lib_client, spoken, path):
    client, api_key, library_id, _ = public_lib_client
    url = path.format(asset=spoken, library=library_id)
    sep = "&" if "?" in url else "?"
    assert client.get(f"{url}{sep}public_library_id={library_id}").status_code == 401
    assert client.get(url, headers={"Authorization": f"Bearer {api_key}"}).status_code == 200


@pytest.mark.slow
def test_a_public_page_doesnt_show_notes(public_lib_client, spoken):
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.put(f"/v1/assets/{spoken}/note", json={"text": "keep this take"}, headers=auth)
    assert r.status_code in (200, 204), r.text
    public = client.get(f"/v1/assets/{spoken}", params={"public_library_id": library_id}).json()
    # Notes are the team's working notes (Robert's call, Oct 8).
    assert not public.get("note") and not public.get("note_author") and not public.get("note_updated_at")
    signed_in = client.get(f"/v1/assets/{spoken}", headers=auth).json()
    assert signed_in["note"] == "keep this take"


@pytest.mark.slow
def test_a_trashed_clips_artifacts_arent_public(public_lib_client):
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _ingest(client, api_key, library_id, "trash/gone.jpg", media_type="image")
    url = f"/v1/assets/{asset_id}/artifacts/proxy"
    assert client.get(url, params={"public_library_id": library_id}).status_code == 200
    assert client.delete(f"/v1/assets/{asset_id}", headers=auth).status_code == 204
    assert client.get(url, params={"public_library_id": library_id}).status_code == 404


@pytest.mark.slow
def test_scene_frames_past_the_public_cap_arent_public(public_lib_client, spoken):
    client, _, library_id, _ = public_lib_client
    r = client.get(f"/v1/assets/{spoken}/artifacts/scene_rep",
                   params={"public_library_id": library_id, "rep_frame_ms": 60_000})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Public projects: visitors get what the project list gives, no more
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shared_clip(public_lib_client):
    """A clip from the PRIVATE library, with GPS, camera and a note, in a public project."""
    import io
    import json as _json

    from PIL import Image

    client, api_key, _, private_library_id = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    buf = io.BytesIO()
    Image.new("RGB", (32, 18)).save(buf, format="JPEG")
    buf.seek(0)
    exif = {"sha256": os.urandom(32).hex(), "gps_lat": 40.7, "gps_lon": -74.0, "camera_make": "Sony"}
    r = client.post("/v1/ingest", data={"library_id": private_library_id, "rel_path": "Clients/ACME/interview_jane.mov",
                                        "file_size": "10", "media_type": "video", "width": "32", "height": "18",
                                        "exif": _json.dumps(exif), "lineage": ingest_made()},
                    files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=auth)
    assert r.status_code == 200, r.text
    asset_id = r.json()["asset_id"]
    assert client.put(f"/v1/assets/{asset_id}/note", json={"text": "internal: reshoot"}, headers=auth).status_code == 200
    r = client.post("/v1/projects", json={"name": "Reel", "asset_ids": [asset_id], "visibility": "public"}, headers=auth)
    assert r.status_code == 201, r.text
    return asset_id, r.json()["project_id"]


@pytest.mark.slow
def test_a_public_projects_clip_detail_is_privacy_stripped(public_lib_client, shared_clip):
    client, api_key, _, private_library_id = public_lib_client
    asset_id, project_id = shared_clip
    r = client.get(f"/v1/assets/{asset_id}", params={"public_project_id": project_id})
    assert r.status_code == 200, r.text
    d = r.json()
    # What the project list gives: shape and time.
    assert (d["asset_id"], d["media_type"], d["width"], d["height"]) == (asset_id, "video", 32, 18)
    # Not where it lives, where it was shot, what shot it, or what the team noted.
    assert d["rel_path"] == "" and d["library_id"] == ""
    for field in ("gps_lat", "gps_lon", "sha256", "camera_make", "camera_model", "proxy_key", "thumbnail_key",
                  "video_preview_key", "note", "note_author", "note_updated_at", "file_size"):
        assert d.get(field) in (None, ""), (field, d.get(field))
    # What's seen or heard still comes through, within the public cap.
    srt = "1\n00:00:02,000 --> 00:00:04,000\nearly words\n\n2\n00:01:00,000 --> 00:01:02,000\nlate words\n"
    auth = {"Authorization": f"Bearer {api_key}"}
    assert client.post(f"/v1/assets/{asset_id}/transcript", json={"source": "manual", "srt": srt, "language": "en"},
                       headers=auth).status_code == 200
    shown = client.get(f"/v1/assets/{asset_id}", params={"public_project_id": project_id}).json()
    assert "early words" in shown["transcript_srt"] and "late words" not in shown["transcript_srt"]
    assert shown["transcript_language"] == "en"
    # Signed in, it's all there.
    full = client.get(f"/v1/assets/{asset_id}", headers={"Authorization": f"Bearer {api_key}"}).json()
    assert full["gps_lat"] == 40.7 and full["note"] == "internal: reshoot" and full["library_id"] == private_library_id


@pytest.mark.slow
def test_project_visitors_cant_reach_library_health_or_a_private_revision(public_lib_client, shared_clip):
    client, _, _, private_library_id = public_lib_client
    _, project_id = shared_clip
    params = {"public_project_id": project_id}
    assert client.get("/v1/libraries/health", params=params).status_code == 401
    assert client.get(f"/v1/libraries/{private_library_id}/revision", params=params).status_code == 404


@pytest.mark.slow
def test_a_public_library_doesnt_show_its_folder_on_the_server(public_lib_client):
    client, api_key, library_id, _ = public_lib_client
    public = client.get(f"/v1/libraries/{library_id}").json()
    assert not public.get("root_path")
    signed_in = client.get(f"/v1/libraries/{library_id}", headers={"Authorization": f"Bearer {api_key}"}).json()
    assert signed_in["root_path"] == "/pub"


@pytest.mark.slow
@pytest.mark.parametrize("param", [{"person_id": "per_x"}, {"favorite": "true"}, {"star_min": "3"}, {"color": "red"},
                                   {"has_rating": "true"}])
def test_asset_pages_refuse_visitors_private_filters(public_lib_client, param):
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/page", params={"library_id": library_id, **param})
    assert r.status_code == 403, (param, r.status_code, r.text)


@pytest.mark.slow
def test_similar_from_a_trashed_clip_isnt_public(public_lib_client):
    client, api_key, library_id, _ = public_lib_client
    asset_id = _ingest(client, api_key, library_id, "sim/trashed.jpg", media_type="image")
    assert client.delete(f"/v1/assets/{asset_id}", headers={"Authorization": f"Bearer {api_key}"}).status_code == 204
    r = client.get("/v1/similar", params={"asset_id": asset_id, "library_id": library_id})
    assert r.status_code == 404


@pytest.mark.slow
def test_public_search_doesnt_read_notes(public_lib_client):
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _ingest(client, api_key, library_id, "notes/clip.mov")
    assert client.put(f"/v1/assets/{asset_id}/note", json={"text": "flamingo budget"}, headers=auth).status_code == 200
    params = [("f", f"library:{library_id}"), ("f", "query:flamingo")]
    assert [i["asset_id"] for i in client.get("/v1/query", params=params).json()["items"]] == []
    assert asset_id in [i["asset_id"] for i in client.get("/v1/query", params=params, headers=auth).json()["items"]]


# ---------------------------------------------------------------------------
# A public library's visitor sees what a public project's does: no location,
# paths or camera details, and can't search by location either.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def located(public_lib_client):
    """A public photo shot somewhere, with a camera."""
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    asset_id = _ingest(client, api_key, library_id, "trips/secret-beach.jpg", media_type="image")
    tenant_id = client.get("/v1/tenant/context", headers=auth).json()["tenant_id"]
    from sqlmodel import Session

    from src.server.database import get_control_session, get_engine_for_url
    from src.server.repository.control_plane import TenantDbRoutingRepository

    with get_control_session() as control:
        url = TenantDbRoutingRepository(control).get_by_tenant_id(tenant_id).connection_string
    with Session(get_engine_for_url(url)) as session:
        session.execute(text(
            "UPDATE assets SET gps_lat = 48.8584, gps_lon = 2.2945, camera_make = 'Acme', camera_model = 'XU1',"
            " lens_model = 'L', iso = 200, aperture = 2.8, focal_length = 35 WHERE asset_id = :a"), {"a": asset_id})
        session.commit()
    return asset_id


_PRIVATE_FIELDS = ("gps_lat", "gps_lon", "camera_make", "camera_model", "lens_model", "iso", "aperture",
                   "focal_length")


def _assert_trimmed(item: dict) -> None:
    for field in _PRIVATE_FIELDS:
        assert item.get(field) is None, (field, item)
    assert item["rel_path"] == ""


@pytest.mark.slow
def test_public_library_detail_is_the_visitor_view(public_lib_client, located):
    client, api_key, library_id, _ = public_lib_client
    r = client.get(f"/v1/assets/{located}", params={"public_library_id": library_id})
    assert r.status_code == 200, r.text
    _assert_trimmed(r.json())
    assert r.json()["thumbnail_key"] is None and r.json()["library_id"] == ""
    signed_in = client.get(f"/v1/assets/{located}", headers={"Authorization": f"Bearer {api_key}"}).json()
    assert signed_in["gps_lat"] == 48.8584 and signed_in["rel_path"] == "trips/secret-beach.jpg"


@pytest.mark.slow
def test_public_library_page_and_query_are_trimmed(public_lib_client, located):
    client, _, library_id, _ = public_lib_client
    page = client.get("/v1/assets/page", params={"library_id": library_id})
    assert page.status_code == 200, page.text
    query = client.get("/v1/query", params=[("f", f"library:{library_id}")])
    assert query.status_code == 200, query.text
    for items in (page.json()["items"], query.json()["items"]):
        mine = [i for i in items if i["asset_id"] == located]
        assert mine, items
        _assert_trimmed(mine[0])


@pytest.mark.slow
@pytest.mark.parametrize("location_filter", ["near:48.8584,2.2945,1", "has_gps:yes", "include_guesses:yes"])
def test_public_queries_cant_search_by_location(public_lib_client, located, location_filter):
    client, _, library_id, _ = public_lib_client
    params = [("f", f"library:{library_id}"), ("f", location_filter)]
    assert client.get("/v1/query", params=params).status_code == 403
    assert client.get("/v1/assets/facets", params=params).status_code == 403


@pytest.mark.slow
def test_public_detail_shows_no_location_of_any_kind(public_lib_client, located):
    """A person's location (ADR-017) is no more public than the file's GPS."""
    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    r = client.put("/v1/assets/locations", json={"asset_ids": [located], "lat": 1.5, "lon": 2.5, "replace": "all"},
                   headers=auth)
    assert r.status_code == 200, r.text
    public = client.get(f"/v1/assets/{located}", params={"public_library_id": library_id}).json()
    for field in ("location", "gps_lat", "gps_lon", "gps_accuracy_m"):
        assert public.get(field) is None, (field, public)
    facets = client.get("/v1/assets/facets", params=[("f", f"library:{library_id}")]).json()
    assert facets["has_gps_count"] == 0 and facets["guess_count"] == 0
    signed_in = client.get(f"/v1/assets/{located}", headers=auth).json()
    assert signed_in["location"]["lat"] == 1.5 and signed_in["gps_lat"] == 48.8584
    assert client.request("DELETE", "/v1/assets/locations", json={"asset_ids": [located]},
                          headers=auth).status_code == 200


@pytest.mark.slow
def test_public_pages_show_no_guess_and_the_producers_routes_refuse_visitors(public_lib_client):
    """A location guess (ADR-017 phase 3) is no more public than the file's GPS."""
    import json as _json

    from tests.machine_lineage import made

    client, api_key, library_id, _ = public_lib_client
    auth = {"Authorization": f"Bearer {api_key}"}
    guessed = _ingest(client, api_key, library_id, "trips/guessed.jpg", media_type="image")
    guess = {"lat": 3.5, "lon": 4.5, "radius_m": 900, "basis": {"summary": "From iPhone photos at 14:02"}}
    r = client.put(f"/v1/assets/locations/guess/{guessed}", json={"guess": guess, "lineage": made("location")},
                   headers=auth)
    assert r.json() == {"result": "stored"}, r.text
    public = client.get(f"/v1/assets/{guessed}", params={"public_library_id": library_id}).json()
    for field in ("location", "gps_lat", "gps_lon", "gps_accuracy_m"):
        assert public.get(field) is None, (field, public)
    assert "3.5" not in _json.dumps(public) and "iPhone" not in _json.dumps(public)
    facets = client.get("/v1/assets/facets", params=[("f", f"library:{library_id}")]).json()
    assert facets["guess_count"] == 0
    assert client.get(f"/v1/assets/{guessed}", headers=auth).json()["location"]["source"] == "time"
    visitor = {"public_library_id": library_id}
    assert client.get(f"/v1/assets/locations/context/{guessed}", params=visitor).status_code in (401, 403)
    assert client.post("/v1/assets/locations/clocks", params=visitor,
                       json={"asset_ids": [guessed]}).status_code in (401, 403)
    assert client.put(f"/v1/assets/locations/guess/{guessed}", params=visitor,
                      json={"guess": None, "lineage": made("location")}).status_code in (401, 403)


@pytest.mark.slow
@pytest.mark.parametrize("location_params", [{"has_gps": "true"}, {"near_lat": "48.8584", "near_lon": "2.2945"}])
def test_public_page_cant_filter_by_location(public_lib_client, located, location_params):
    client, _, library_id, _ = public_lib_client
    r = client.get("/v1/assets/page", params={"library_id": library_id, **location_params})
    assert r.status_code == 403, r.text


@pytest.mark.slow
def test_public_facets_count_no_locations_or_cameras(public_lib_client, located):
    client, api_key, library_id, _ = public_lib_client
    params = [("f", f"library:{library_id}")]
    public = client.get("/v1/assets/facets", params=params).json()
    assert public["has_gps_count"] == 0 and public["camera_makes"] == [] and public["iso_range"] == [None, None]
    signed_in = client.get("/v1/assets/facets", params=params, headers={"Authorization": f"Bearer {api_key}"}).json()
    assert signed_in["has_gps_count"] >= 1 and "Acme" in signed_in["camera_makes"]


@pytest.mark.slow
@pytest.mark.parametrize("camera_filter", ["camera_make:Acme", "lens:L", "iso:100-400", "has_exposure:yes"])
def test_public_queries_cant_filter_by_camera(public_lib_client, located, camera_filter):
    """The visitor view hides camera details, so filters mustn't reveal them."""
    client, _, library_id, _ = public_lib_client
    params = [("f", f"library:{library_id}"), ("f", camera_filter)]
    assert client.get("/v1/query", params=params).status_code == 403
    assert client.get("/v1/assets/facets", params=params).status_code == 403
    r = client.get("/v1/assets/page", params={"library_id": library_id, "camera_make": "Acme"})
    assert r.status_code == 403, r.text
