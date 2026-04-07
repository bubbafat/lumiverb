"""Ratings API integration tests. Uses testcontainers Postgres + tenant DB."""

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
    """Helper: ingest a minimal asset, return asset_id."""
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


@pytest.fixture(scope="module")
def ratings_env():
    """Two testcontainers Postgres: control + tenant. Yield (client, api_key, library_id)."""
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
        os.environ["ADMIN_KEY"] = "test-admin-ratings"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "RatingsTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-ratings"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

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
                cr = client.post(
                    "/v1/libraries",
                    json={"name": "RatingTestLib", "root_path": "/tmp/rating-test"},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                assert cr.status_code == 200
                library_id = cr.json()["library_id"]
                yield client, api_key, library_id, tenant_id

        _engines.clear()


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Single asset rating
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_rate_asset_favorite(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "fav1.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["favorite"] is True
    assert data["stars"] == 0
    assert data["color"] is None
    assert data["asset_id"] == asset_id


@pytest.mark.slow
def test_rate_asset_stars(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "stars1.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"stars": 4},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["stars"] == 4


@pytest.mark.slow
def test_rate_asset_color(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "color1.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"color": "red"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["color"] == "red"


@pytest.mark.slow
def test_rate_asset_all_three(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "all3.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True, "stars": 5, "color": "green"},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["favorite"] is True
    assert data["stars"] == 5
    assert data["color"] == "green"


@pytest.mark.slow
def test_rate_asset_partial_update(ratings_env):
    """Setting stars doesn't clear existing favorite."""
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "partial1.jpg")

    # Set favorite
    client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True},
        headers=_headers(api_key),
    )
    # Update stars only
    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"stars": 3},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["favorite"] is True  # unchanged
    assert data["stars"] == 3


@pytest.mark.slow
def test_rate_asset_clear_to_default_deletes_row(ratings_env):
    """Clearing all fields to defaults removes the row."""
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "clear1.jpg")

    # Set a rating
    client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True, "stars": 3, "color": "blue"},
        headers=_headers(api_key),
    )
    # Clear everything
    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": False, "stars": 0, "color": None},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["favorite"] is False
    assert data["stars"] == 0
    assert data["color"] is None

    # Lookup should return empty
    lookup = client.post(
        "/v1/assets/ratings/lookup",
        json={"asset_ids": [asset_id]},
        headers=_headers(api_key),
    )
    assert lookup.status_code == 200
    assert asset_id not in lookup.json()["ratings"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_rate_invalid_stars(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "badstars.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"stars": 6},
        headers=_headers(api_key),
    )
    assert r.status_code == 422


@pytest.mark.slow
def test_rate_invalid_color(ratings_env):
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "badcolor.jpg")

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"color": "pink"},
        headers=_headers(api_key),
    )
    assert r.status_code == 422


@pytest.mark.slow
def test_rate_nonexistent_asset(ratings_env):
    client, api_key, _, _ = ratings_env

    r = client.put(
        "/v1/assets/ast_nonexistent/rating",
        json={"favorite": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_rate_trashed_asset(ratings_env):
    """Cannot rate a trashed asset."""
    client, api_key, library_id, _ = ratings_env
    asset_id = _ingest_asset(client, api_key, library_id, "trashed_rate.jpg")

    # Trash the asset
    client.request(
        "DELETE",
        "/v1/assets",
        json={"asset_ids": [asset_id]},
        headers=_headers(api_key),
    )

    r = client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Bulk read
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_lookup_ratings(ratings_env):
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "lookup1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "lookup2.jpg")
    a3 = _ingest_asset(client, api_key, library_id, "lookup3.jpg")

    # Rate a1 and a2, leave a3 unrated
    client.put(
        f"/v1/assets/{a1}/rating",
        json={"favorite": True, "stars": 5},
        headers=_headers(api_key),
    )
    client.put(
        f"/v1/assets/{a2}/rating",
        json={"color": "orange"},
        headers=_headers(api_key),
    )

    r = client.post(
        "/v1/assets/ratings/lookup",
        json={"asset_ids": [a1, a2, a3]},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ratings = r.json()["ratings"]
    assert a1 in ratings
    assert ratings[a1]["favorite"] is True
    assert ratings[a1]["stars"] == 5
    assert a2 in ratings
    assert ratings[a2]["color"] == "orange"
    assert a3 not in ratings  # unrated assets omitted


# ---------------------------------------------------------------------------
# Batch update
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_batch_rate_assets(ratings_env):
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "batch1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "batch2.jpg")

    r = client.put(
        "/v1/assets/ratings",
        json={"asset_ids": [a1, a2], "favorite": True, "stars": 3},
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 2

    # Verify
    lookup = client.post(
        "/v1/assets/ratings/lookup",
        json={"asset_ids": [a1, a2]},
        headers=_headers(api_key),
    )
    ratings = lookup.json()["ratings"]
    assert ratings[a1]["favorite"] is True
    assert ratings[a1]["stars"] == 3
    assert ratings[a2]["favorite"] is True
    assert ratings[a2]["stars"] == 3


@pytest.mark.slow
def test_batch_rate_nonexistent_asset(ratings_env):
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "batchbad1.jpg")

    r = client.put(
        "/v1/assets/ratings",
        json={"asset_ids": [a1, "ast_nonexistent"], "favorite": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_batch_empty_asset_ids(ratings_env):
    client, api_key, _, _ = ratings_env

    r = client.put(
        "/v1/assets/ratings",
        json={"asset_ids": [], "favorite": True},
        headers=_headers(api_key),
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# User cleanup
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_user_deletion_cleans_ratings(ratings_env):
    """When a user is deleted, all their ratings are removed."""
    client, api_key, library_id, tenant_id = ratings_env

    asset_id = _ingest_asset(client, api_key, library_id, "userclean.jpg")

    # Rate the asset
    client.put(
        f"/v1/assets/{asset_id}/rating",
        json={"favorite": True, "stars": 5, "color": "purple"},
        headers=_headers(api_key),
    )

    # Verify it's there
    lookup = client.post(
        "/v1/assets/ratings/lookup",
        json={"asset_ids": [asset_id]},
        headers=_headers(api_key),
    )
    assert asset_id in lookup.json()["ratings"]

    # Create a user, rate the asset as that user, then delete the user
    import bcrypt
    from src.server.database import get_control_session
    from src.server.repository.control_plane import UserRepository
    from ulid import ULID

    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.create(
            tenant_id=tenant_id,
            email=f"test-{ULID()}@example.com",
            password_hash=bcrypt.hashpw(b"testpassword12", bcrypt.gensalt()).decode(),
            role="editor",
        )
        user_id = user.user_id

    # We can't easily rate as this new user via API (would need a JWT),
    # so insert the rating directly
    from src.server.database import get_engine_for_url
    from sqlmodel import Session
    from src.server.models.tenant import AssetRating
    from src.shared.utils import utcnow

    # Get tenant DB URL from routing table
    with get_control_session() as session:
        from src.server.repository.control_plane import TenantDbRoutingRepository
        routing_repo = TenantDbRoutingRepository(session)
        row = routing_repo.get_by_tenant_id(tenant_id)
        tenant_url = row.connection_string

    engine = get_engine_for_url(tenant_url)
    with Session(engine) as tsession:
        rating = AssetRating(
            user_id=user_id,
            asset_id=asset_id,
            favorite=True,
            stars=4,
            color="red",
            updated_at=utcnow(),
        )
        tsession.add(rating)
        tsession.commit()

    # Verify the rating exists
    with Session(engine) as tsession:
        from src.server.repository.tenant import RatingRepository
        rr = RatingRepository(tsession)
        assert rr.get_for_asset(user_id, asset_id) is not None

    # Delete the user via API (as admin)
    r = client.delete(
        f"/v1/users/{user_id}",
        headers=_headers(api_key),
    )
    assert r.status_code == 204

    # Verify ratings are gone
    with Session(engine) as tsession:
        rr = RatingRepository(tsession)
        assert rr.get_for_asset(user_id, asset_id) is None

    # Original API key user's ratings should still be there
    lookup = client.post(
        "/v1/assets/ratings/lookup",
        json={"asset_ids": [asset_id]},
        headers=_headers(api_key),
    )
    assert asset_id in lookup.json()["ratings"]


# ---------------------------------------------------------------------------
# Browse filters
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_browse_filter_favorite(ratings_env):
    """Browse with favorite=true only returns favorited assets."""
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "filt_fav1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "filt_fav2.jpg")

    # Favorite a1 only
    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))

    r = client.get(
        f"/v1/assets/page?library_id={library_id}&favorite=true",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


@pytest.mark.slow
def test_browse_filter_star_min(ratings_env):
    """Browse with star_min filters to assets with at least N stars."""
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "filt_star1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "filt_star2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"stars": 5}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a2}/rating", json={"stars": 2}, headers=_headers(api_key))

    r = client.get(
        f"/v1/assets/page?library_id={library_id}&star_min=4",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


@pytest.mark.slow
def test_browse_filter_color(ratings_env):
    """Browse with color filter returns matching assets."""
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "filt_col1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "filt_col2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"color": "red"}, headers=_headers(api_key))
    client.put(f"/v1/assets/{a2}/rating", json={"color": "blue"}, headers=_headers(api_key))

    r = client.get(
        f"/v1/assets/page?library_id={library_id}&color=red",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 not in ids


@pytest.mark.slow
def test_favorites_endpoint(ratings_env):
    """GET /v1/assets/favorites returns favorited assets across libraries."""
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "favep1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "favep2.jpg")

    # Favorite a1 only
    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))

    r = client.get("/v1/assets/favorites", headers=_headers(api_key))
    assert r.status_code == 200
    data = r.json()
    ids = [i["asset_id"] for i in data["items"]]
    assert a1 in ids
    assert a2 not in ids
    # Check library_name is present
    for item in data["items"]:
        if item["asset_id"] == a1:
            assert item["library_name"] != ""


@pytest.mark.slow
def test_favorites_endpoint_empty(ratings_env):
    """Favorites endpoint returns empty when no favorites."""
    client, api_key, library_id, _ = ratings_env
    # Just check it doesn't error
    r = client.get("/v1/assets/favorites", headers=_headers(api_key))
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


@pytest.mark.slow
def test_browse_filter_no_rating_filters_returns_all(ratings_env):
    """Without rating filters, rated and unrated assets both appear."""
    client, api_key, library_id, _ = ratings_env
    a1 = _ingest_asset(client, api_key, library_id, "filt_all1.jpg")
    a2 = _ingest_asset(client, api_key, library_id, "filt_all2.jpg")

    client.put(f"/v1/assets/{a1}/rating", json={"favorite": True}, headers=_headers(api_key))
    # a2 has no rating

    r = client.get(
        f"/v1/assets/page?library_id={library_id}",
        headers=_headers(api_key),
    )
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert a1 in ids
    assert a2 in ids
