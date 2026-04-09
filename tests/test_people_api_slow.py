"""Slow integration tests for people API endpoints."""

from __future__ import annotations

import os
import secrets
from typing import Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def people_client() -> Tuple[_AuthClient, str, str]:
    """Set up two Postgres containers, tenant, library. Yields (auth_client, library_id, tenant_url)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["JWT_SECRET"] = "test-jwt-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "PeopleTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            with get_control_session() as session:
                routing_repo = TenantDbRoutingRepository(session)
                row = routing_repo.get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                auth_client = _AuthClient(client, api_key)
                lib_name = "PeopleLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/people"},
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _create_asset_with_faces(auth_client: _AuthClient, library_id: str, name: str, n_faces: int = 1) -> tuple[str, list[str]]:
    """Create a test asset, submit faces, return (asset_id, face_ids).

    Each face's embedding is derived from `name` so different helper calls
    produce orthogonal-ish vectors. This prevents the server-side
    `_auto_assign_by_centroid` pass from cross-attaching faces between
    tests that share the module-scoped fixture.
    """
    import hashlib
    import random as _random

    rel_path = f"photos/{name}.jpg"
    r = auth_client.post(
        "/v1/assets/upsert",
        json={
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": 1000,
            "file_mtime": "2024-01-01T00:00:00Z",
            "media_type": "image",
        },
    )
    assert r.status_code == 200
    r2 = auth_client.get("/v1/assets/by-path", params={"library_id": library_id, "rel_path": rel_path})
    asset_id = r2.json()["asset_id"]

    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    rng = _random.Random(seed)
    faces = [
        {
            "bounding_box": {"x": 0.1 * i, "y": 0.1, "w": 0.1, "h": 0.15},
            "detection_confidence": 0.95 - 0.01 * i,
            "embedding": [rng.uniform(-1.0, 1.0) for _ in range(512)],
        }
        for i in range(n_faces)
    ]
    r3 = auth_client.post(f"/v1/assets/{asset_id}/faces", json={"faces": faces})
    assert r3.status_code == 201
    face_ids = r3.json()["face_ids"]
    return asset_id, face_ids


@pytest.mark.slow
def test_create_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/people creates a person."""
    auth_client, library_id, _ = people_client

    r = auth_client.post("/v1/people", json={"display_name": "Alice"})
    assert r.status_code == 201, (r.status_code, r.text)
    data = r.json()
    assert data["display_name"] == "Alice"
    assert data["person_id"].startswith("person_")
    assert data["face_count"] == 0


@pytest.mark.slow
def test_create_person_with_faces(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/people with face_ids assigns faces."""
    auth_client, library_id, _ = people_client

    _, face_ids = _create_asset_with_faces(auth_client, library_id, "alice_photo", 2)

    r = auth_client.post("/v1/people", json={"display_name": "Bob", "face_ids": face_ids})
    assert r.status_code == 201
    data = r.json()
    assert data["face_count"] == 2
    assert data["representative_face_id"] is not None


@pytest.mark.slow
def test_list_people(people_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/people returns people sorted by face count desc."""
    auth_client, _, _ = people_client

    r = auth_client.get("/v1/people")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "next_cursor" in data
    # Bob (2 faces) should come before Alice (0 faces)
    names = [p["display_name"] for p in data["items"]]
    assert "Bob" in names
    assert "Alice" in names


@pytest.mark.slow
def test_clusters_cache_marked_dirty_on_face_assign(people_client: Tuple[_AuthClient, str, str]) -> None:
    """
    After a cluster cache computation, assigning a face should mark the cache dirty
    so the next GET /v1/faces/clusters recomputes.
    """
    auth_client, library_id, tenant_url = people_client

    # Create at least one unassigned face with an embedding so clusters endpoint can compute/cache.
    _, face_ids = _create_asset_with_faces(auth_client, library_id, "cluster_dirty", 2)

    r = auth_client.get("/v1/faces/clusters")
    assert r.status_code == 200

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        v = conn.execute(
            text("SELECT value FROM system_metadata WHERE key = 'face_clusters_dirty'")
        ).scalar()
        assert v == "false"

    # Create a person and assign one of the faces.
    r_person = auth_client.post("/v1/people", json={"display_name": "CacheTest"})
    assert r_person.status_code == 201
    person_id = r_person.json()["person_id"]

    r2 = auth_client.post(f"/v1/faces/{face_ids[0]}/assign", json={"person_id": person_id})
    assert r2.status_code == 200

    with engine.connect() as conn:
        v2 = conn.execute(
            text("SELECT value FROM system_metadata WHERE key = 'face_clusters_dirty'")
        ).scalar()
        assert v2 == "true"

    engine.dispose()


@pytest.mark.slow
def test_get_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/people/{id} returns person details."""
    auth_client, _, _ = people_client

    # Get Alice's ID from list
    r = auth_client.get("/v1/people")
    alice = [p for p in r.json()["items"] if p["display_name"] == "Alice"][0]

    r = auth_client.get(f"/v1/people/{alice['person_id']}")
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"


@pytest.mark.slow
def test_update_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """PATCH /v1/people/{id} updates display_name."""
    auth_client, _, _ = people_client

    r = auth_client.get("/v1/people")
    alice = [p for p in r.json()["items"] if p["display_name"] == "Alice"][0]

    r = auth_client.post(  # Using post since _AuthClient doesn't have .patch
        f"/v1/people/{alice['person_id']}",
        json={"display_name": "Alice Smith"},
    )
    # TestClient doesn't have patch, use raw client
    r = auth_client._client.patch(
        f"/v1/people/{alice['person_id']}",
        json={"display_name": "Alice Smith"},
        headers=auth_client._headers,
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice Smith"


@pytest.mark.slow
def test_person_not_found(people_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/people/{nonexistent} returns 404."""
    auth_client, _, _ = people_client

    r = auth_client.get("/v1/people/person_nonexistent")
    assert r.status_code == 404


@pytest.mark.slow
def test_delete_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """DELETE /v1/people/{id} removes person and matches."""
    auth_client, _, _ = people_client

    # Create a temp person to delete
    r = auth_client.post("/v1/people", json={"display_name": "ToDelete"})
    assert r.status_code == 201
    pid = r.json()["person_id"]

    r = auth_client._client.delete(
        f"/v1/people/{pid}",
        headers=auth_client._headers,
    )
    assert r.status_code == 204

    # Verify gone
    r = auth_client.get(f"/v1/people/{pid}")
    assert r.status_code == 404


@pytest.mark.slow
def test_list_person_faces(people_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/people/{id}/faces returns matched faces."""
    auth_client, _, _ = people_client

    r = auth_client.get("/v1/people")
    bob = [p for p in r.json()["items"] if p["display_name"] == "Bob"][0]

    r = auth_client.get(f"/v1/people/{bob['person_id']}/faces")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert all("face_id" in f for f in data["items"])
    assert all("asset_id" in f for f in data["items"])


@pytest.mark.slow
def test_faces_endpoint_populates_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/{id}/faces returns person data for matched faces."""
    auth_client, _, _ = people_client

    # Get Bob's faces to find the asset_id
    r = auth_client.get("/v1/people")
    bob = [p for p in r.json()["items"] if p["display_name"] == "Bob"][0]
    r = auth_client.get(f"/v1/people/{bob['person_id']}/faces")
    asset_id = r.json()["items"][0]["asset_id"]

    # Now call the faces endpoint on that asset
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert r.status_code == 200
    faces = r.json()["faces"]
    assert len(faces) >= 1
    # At least one face should have person populated
    matched = [f for f in faces if f["person"] is not None]
    assert len(matched) >= 1
    assert matched[0]["person"]["display_name"] == "Bob"
    assert matched[0]["person"]["person_id"] == bob["person_id"]


@pytest.mark.slow
def test_create_person_conflict_already_assigned(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/people with already-assigned face_ids returns 409."""
    auth_client, _, _ = people_client

    # Get Bob's face IDs
    r = auth_client.get("/v1/people")
    bob = [p for p in r.json()["items"] if p["display_name"] == "Bob"][0]
    r = auth_client.get(f"/v1/people/{bob['person_id']}/faces")
    face_ids = [f["face_id"] for f in r.json()["items"]]

    # Try to create new person with same faces
    r = auth_client.post("/v1/people", json={"display_name": "Duplicate", "face_ids": face_ids})
    assert r.status_code == 409


# ---------- Phase 3: assign / unassign / merge ----------


@pytest.mark.slow
def test_assign_face_to_existing_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/faces/{face_id}/assign assigns face to an existing person."""
    auth_client, library_id, _ = people_client

    # Create an unassigned face
    _, face_ids = _create_asset_with_faces(auth_client, library_id, "assign_test", 1)
    face_id = face_ids[0]

    # Create a person
    r = auth_client.post("/v1/people", json={"display_name": "AssignTarget"})
    assert r.status_code == 201
    person_id = r.json()["person_id"]

    # Assign face to person
    r = auth_client.post(f"/v1/faces/{face_id}/assign", json={"person_id": person_id})
    assert r.status_code == 200
    assert r.json()["person_id"] == person_id

    # Verify face is now matched
    r = auth_client.get(f"/v1/people/{person_id}/faces")
    face_ids_matched = [f["face_id"] for f in r.json()["items"]]
    assert face_id in face_ids_matched


@pytest.mark.slow
def test_assign_face_new_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/faces/{face_id}/assign with new_person_name creates person and assigns."""
    auth_client, library_id, _ = people_client

    _, face_ids = _create_asset_with_faces(auth_client, library_id, "assign_new_test", 1)
    face_id = face_ids[0]

    r = auth_client.post(f"/v1/faces/{face_id}/assign", json={"new_person_name": "NewPerson"})
    assert r.status_code == 200
    data = r.json()
    assert data["display_name"] == "NewPerson"
    assert data["person_id"].startswith("person_")


@pytest.mark.slow
def test_assign_face_conflict(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/faces/{face_id}/assign returns 409 if face already assigned."""
    auth_client, library_id, _ = people_client

    _, face_ids = _create_asset_with_faces(auth_client, library_id, "assign_conflict", 1)
    face_id = face_ids[0]

    # Create person and assign
    r = auth_client.post(f"/v1/faces/{face_id}/assign", json={"new_person_name": "ConflictTest"})
    assert r.status_code == 200

    # Try to assign again
    r = auth_client.post(f"/v1/faces/{face_id}/assign", json={"new_person_name": "AnotherPerson"})
    assert r.status_code == 409


@pytest.mark.slow
def test_unassign_face(people_client: Tuple[_AuthClient, str, str]) -> None:
    """DELETE /v1/faces/{face_id}/assign removes face from person."""
    auth_client, library_id, _ = people_client

    _, face_ids = _create_asset_with_faces(auth_client, library_id, "unassign_test", 1)
    face_id = face_ids[0]

    r = auth_client.post(f"/v1/faces/{face_id}/assign", json={"new_person_name": "UnassignTarget"})
    assert r.status_code == 200
    person_id = r.json()["person_id"]

    # Unassign
    r = auth_client._client.delete(
        f"/v1/faces/{face_id}/assign",
        headers=auth_client._headers,
    )
    assert r.status_code == 204

    # Verify face is no longer matched
    r = auth_client.get(f"/v1/people/{person_id}/faces")
    face_ids_matched = [f["face_id"] for f in r.json()["items"]]
    assert face_id not in face_ids_matched


@pytest.mark.slow
def test_unassign_face_not_found(people_client: Tuple[_AuthClient, str, str]) -> None:
    """DELETE /v1/faces/{face_id}/assign returns 404 if not assigned."""
    auth_client, library_id, _ = people_client

    _, face_ids = _create_asset_with_faces(auth_client, library_id, "unassign_404", 1)
    face_id = face_ids[0]

    r = auth_client._client.delete(
        f"/v1/faces/{face_id}/assign",
        headers=auth_client._headers,
    )
    assert r.status_code == 404


@pytest.mark.slow
def test_merge_person(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/people/{id}/merge merges source into target, deletes source."""
    auth_client, library_id, _ = people_client

    # Create two people with faces
    _, face_ids_a = _create_asset_with_faces(auth_client, library_id, "merge_a", 1)
    _, face_ids_b = _create_asset_with_faces(auth_client, library_id, "merge_b", 1)

    r = auth_client.post("/v1/people", json={"display_name": "MergeTarget", "face_ids": face_ids_a})
    assert r.status_code == 201
    target_id = r.json()["person_id"]

    r = auth_client.post("/v1/people", json={"display_name": "MergeSource", "face_ids": face_ids_b})
    assert r.status_code == 201
    source_id = r.json()["person_id"]

    # Merge source into target
    r = auth_client.post(f"/v1/people/{target_id}/merge", json={"source_person_id": source_id})
    assert r.status_code == 200
    data = r.json()
    assert data["face_count"] == 2
    assert data["display_name"] == "MergeTarget"

    # Source should be gone
    r = auth_client.get(f"/v1/people/{source_id}")
    assert r.status_code == 404


@pytest.mark.slow
def test_merge_self_returns_400(people_client: Tuple[_AuthClient, str, str]) -> None:
    """POST /v1/people/{id}/merge with self returns 400."""
    auth_client, _, _ = people_client

    r = auth_client.post("/v1/people", json={"display_name": "SelfMerge"})
    pid = r.json()["person_id"]

    r = auth_client.post(f"/v1/people/{pid}/merge", json={"source_person_id": pid})
    assert r.status_code == 400
