"""Slow integration tests for face detection API endpoints.

A photo's faces are saved as the scheduler's faces producer saves them
(POST /v1/assets/batch-faces, tests/faces.py)."""

from __future__ import annotations

import os
import secrets
from typing import Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from sqlalchemy import create_engine, text
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.repository.tenant import AssetRepository, LibraryRepository
from tests.clips import make_clip
from tests.conftest import PG_IMAGE, _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations
from tests.faces import face_count, plant_faces
from tests.machine_lineage import made
from tests.producer_fakes import due


@pytest.fixture(scope="module")
def face_client() -> Tuple[_AuthClient, str, str]:
    """Set up two Postgres containers, tenant, library. Yields (auth_client, library_id, tenant_url)."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer(PG_IMAGE) as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        _run_control_migrations(control_url)

        u = make_url(control_url)
        tenant_tpl = str(u.set(database="{tenant_id}"))
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = tenant_tpl
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.accounts.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "FaceDetectionTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
                data = r.json()
                tenant_id = data["tenant_id"]
                api_key = data["api_key"]

        with PostgresContainer(PG_IMAGE) as tenant_postgres:
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
                lib_name = "FaceLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/faces"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _create_asset(tenant_url: str, library_id: str, name: str) -> str:
    """Create a test asset and return its asset_id."""
    return make_clip(tenant_url, library_id, f"photos/{name}.jpg")


def _with_proxy(tenant_url: str, asset_id: str) -> None:
    """The photo's proxy, made: what its faces are found in, so the scheduler hands it out."""
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE assets SET proxy_key = :k WHERE asset_id = :a"),
                     {"k": f"proxies/{asset_id}.webp", "a": asset_id})
    engine.dispose()


@pytest.mark.slow
def test_submit_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Saving a photo's faces (POST /v1/assets/batch-faces) creates face records and sets face_count."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_submit")

    face_ids = plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
            "detection_confidence": 0.97,
            "embedding": [0.1] * 512,
        },
        {
            "bounding_box": {"x": 0.5, "y": 0.3, "w": 0.1, "h": 0.15},
            "detection_confidence": 0.85,
            "embedding": [0.2] * 512,
        },
    ], detection_model="insightface", detection_model_version="buffalo_l")
    assert face_count(auth_client, asset_id) == 2
    assert len(set(face_ids)) == 2


@pytest.mark.slow
def test_list_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/{id}/faces returns submitted faces."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_list")

    # Submit faces
    plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3},
            "detection_confidence": 0.95,
            "embedding": [0.5] * 512,
        },
    ])

    # List faces
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert len(data["faces"]) == 1
    face = data["faces"][0]
    assert face["bounding_box"]["x"] == pytest.approx(0.1)
    assert face["detection_confidence"] == pytest.approx(0.95)
    assert face["person"] is None


@pytest.mark.slow
def test_submit_faces_idempotent(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Resubmitting faces replaces the old ones."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_idempotent")

    # First submission: 2 faces
    plant_faces(auth_client, asset_id, [
        {"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9},
        {"bounding_box": {"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}, "detection_confidence": 0.8},
    ])

    # Second submission: 1 face (replaces)
    plant_faces(auth_client, asset_id, [
        {"bounding_box": {"x": 0.3, "y": 0.3, "w": 0.2, "h": 0.2}, "detection_confidence": 0.99},
    ])
    assert face_count(auth_client, asset_id) == 1

    # Verify only 1 face exists
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert len(r.json()["faces"]) == 1


@pytest.mark.slow
def test_submit_no_faces_sets_zero(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Submitting empty faces array sets face_count=0 (processed, no faces)."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_zero")

    assert plant_faces(auth_client, asset_id, []) == []
    assert face_count(auth_client, asset_id) == 0


@pytest.mark.slow
def test_submit_faces_not_found(face_client: Tuple[_AuthClient, str, str]) -> None:
    """An unknown asset's faces are skipped, nothing saved."""
    auth_client, _, _ = face_client
    r = auth_client.post(
        "/v1/assets/batch-faces",
        json={"items": [{"asset_id": "nonexistent_asset_id", "faces": []}], "lineage": made("faces")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"processed": 0, "skipped": 1}


@pytest.mark.slow
def test_list_faces_not_found(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/{id}/faces returns 404 for unknown asset."""
    auth_client, _, _ = face_client
    r = auth_client.get("/v1/assets/nonexistent_asset_id/faces")
    assert r.status_code == 404


@pytest.mark.slow
def test_repair_summary_missing_faces(face_client: Tuple[_AuthClient, str, str]) -> None:
    """repair-summary includes missing_faces count."""
    auth_client, library_id, tenant_url = face_client

    # Create an asset without face detection
    asset_id = _create_asset(tenant_url, library_id, "face_repair_summary")

    r = auth_client.get(f"/v1/assets/repair-summary?library_id={library_id}")
    assert r.status_code == 200
    data = r.json()
    assert "missing_faces" in data
    # At least this asset should be missing faces
    assert data["missing_faces"] >= 1


@pytest.mark.slow
def test_the_scheduler_hands_out_photos_whose_faces_werent_looked_for(face_client: Tuple[_AuthClient, str, str]) -> None:
    """The faces producer is handed the photos not looked at yet (its queue: missing_faces)."""
    auth_client, library_id, tenant_url = face_client

    # Create asset and submit faces for it
    asset_with = _create_asset(tenant_url, library_id, "face_page_with")
    plant_faces(auth_client, asset_with,
                [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}])

    # Create asset without faces
    asset_without = _create_asset(tenant_url, library_id, "face_page_without")
    for asset_id in (asset_with, asset_without):
        _with_proxy(tenant_url, asset_id)

    # The scheduler's queue should include the unprocessed asset
    ids = [c["asset_id"] for c in due(tenant_url, "faces", [library_id])]
    assert asset_without in ids
    assert asset_with not in ids


@pytest.mark.slow
def test_submit_faces_without_embedding(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Faces can be submitted without embedding (embedding=null)."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_no_embed")

    plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
            "detection_confidence": 0.88,
        },
    ])
    assert face_count(auth_client, asset_id) == 1

    # Verify it's retrievable
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert len(r.json()["faces"]) == 1


@pytest.mark.slow
def test_has_faces_filter_on_a_librarys_clips(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/query?f=library:…&f=has_faces:yes filters to assets with detected faces."""
    auth_client, library_id, tenant_url = face_client

    # Asset with faces
    asset_with = _create_asset(tenant_url, library_id, "has_faces_with")
    plant_faces(auth_client, asset_with,
                [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}])

    # Asset with no faces (processed, face_count=0)
    asset_zero = _create_asset(tenant_url, library_id, "has_faces_zero")
    plant_faces(auth_client, asset_zero, [])

    # has_faces:yes -> only asset_with
    r = auth_client.get("/v1/query", params={"f": [f"library:{library_id}", "has_faces:yes"], "limit": 500})
    assert r.status_code == 200
    items = r.json()["items"]
    ids = [i["asset_id"] for i in items]
    assert asset_with in ids
    assert asset_zero not in ids
    # Regression: face_count must be in the listing for the lightbox overlay
    matched = [i for i in items if i["asset_id"] == asset_with][0]
    assert "face_count" in matched, "face_count missing from the listing"
    assert matched["face_count"] == 1

    # has_faces:no -> includes asset_zero and unprocessed
    r = auth_client.get("/v1/query", params={"f": [f"library:{library_id}", "has_faces:no"], "limit": 500})
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert asset_zero in ids
    assert asset_with not in ids


@pytest.mark.slow
def test_has_faces_filter_on_browse(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/query?f=has_faces:yes filters via SQL in UnifiedBrowseRepository."""
    auth_client, library_id, tenant_url = face_client

    # Asset with faces
    asset_with = _create_asset(tenant_url, library_id, "browse_faces_with")
    plant_faces(auth_client, asset_with,
            [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}])

    # Asset without faces (processed)
    asset_zero = _create_asset(tenant_url, library_id, "browse_faces_zero")
    plant_faces(auth_client, asset_zero, [])

    # has_faces=true -> only asset_with
    r = auth_client.get("/v1/query?f=has_faces:yes")
    assert r.status_code == 200
    ids = [i["asset_id"] for i in r.json()["items"]]
    assert asset_with in ids
    assert asset_zero not in ids


@pytest.mark.slow
def test_has_faces_filter_on_search(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/query?f=query:…&f=has_faces:yes filters text search results."""
    auth_client, library_id, tenant_url = face_client

    import json as _json

    # Create asset with faces + metadata (so Postgres search can find it)
    asset_with = _create_asset(tenant_url, library_id, "search_faces_sunset")
    plant_faces(auth_client, asset_with,
            [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}])
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, generated_at, data) "
            "VALUES (:mid, :aid, 'test', '1', NOW(), :data)"
        ), {"mid": "meta_sf_" + asset_with[:8], "aid": asset_with,
            "data": _json.dumps({"description": "sunset over mountains", "tags": ["sunset"]})})
    engine.dispose()

    # Create asset without faces but also searchable
    asset_no = _create_asset(tenant_url, library_id, "search_nofaces_sunset")
    plant_faces(auth_client, asset_no, [])
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset_metadata (metadata_id, asset_id, model_id, model_version, generated_at, data) "
            "VALUES (:mid, :aid, 'test', '1', NOW(), :data)"
        ), {"mid": "meta_snf_" + asset_no[:8], "aid": asset_no,
            "data": _json.dumps({"description": "sunset at the beach", "tags": ["sunset"]})})
    engine.dispose()

    # Search has their descriptions once the sweep writes them.
    r = auth_client.post("/v1/upkeep/search-sync")
    assert r.status_code == 200, r.text

    r = auth_client.get("/v1/query", params={"f": ["query:sunset"]})
    assert r.status_code == 200
    hit_ids = [h["asset_id"] for h in r.json()["items"]]
    assert asset_with in hit_ids and asset_no in hit_ids  # both match the words

    r = auth_client.get("/v1/query", params={"f": ["query:sunset", "has_faces:yes"]})
    assert r.status_code == 200
    data = r.json()
    hit_ids = [h["asset_id"] for h in data["items"]]
    assert asset_with in hit_ids
    assert asset_no not in hit_ids


@pytest.mark.slow
def test_facets_has_face_count(face_client: Tuple[_AuthClient, str, str]) -> None:
    """GET /v1/assets/facets returns has_face_count field."""
    auth_client, library_id, tenant_url = face_client

    # Create an asset with faces
    asset_id = _create_asset(tenant_url, library_id, "facets_face")
    plant_faces(auth_client, asset_id,
            [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9}])

    r = auth_client.get(f"/v1/assets/facets?library_id={library_id}")
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert "has_face_count" in data
    assert data["has_face_count"] >= 1


@pytest.mark.slow
def test_list_faces_response_shape(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Regression: GET /v1/assets/{id}/faces response shape matches what the lightbox expects."""
    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_shape")

    plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.25},
            "detection_confidence": 0.92,
            "embedding": [0.3] * 512,
        },
    ])

    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert r.status_code == 200
    data = r.json()
    assert "faces" in data
    face = data["faces"][0]
    # All fields the lightbox overlay depends on
    assert "face_id" in face
    assert "bounding_box" in face
    bbox = face["bounding_box"]
    for key in ("x", "y", "w", "h"):
        assert key in bbox, f"bounding_box missing '{key}'"
        assert isinstance(bbox[key], (int, float))
    assert "detection_confidence" in face
    assert "person" in face  # null until clustering ships


@pytest.mark.slow
def test_resubmit_faces_with_person_assignment(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Resubmitting faces succeeds even when faces are assigned to a person.

    Regression: submit_faces must clear face_person_matches and null out
    people.representative_face_id before deleting old faces, otherwise
    FK constraints block the delete.
    """
    import numpy as np

    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_resubmit_person")

    # Use a random embedding to avoid auto-assignment from other tests' centroids
    rng = np.random.default_rng(42)
    emb1 = (rng.standard_normal(512)).tolist()

    # Submit initial faces with embeddings
    [face_id] = plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3},
            "detection_confidence": 0.95,
            "embedding": emb1,
        },
    ])

    # Assign face to a new person (creates face_person_match + sets representative_face_id)
    r = auth_client.post(
        f"/v1/faces/{face_id}/assign",
        json={"new_person_name": "Test Person Resubmit"},
    )
    assert r.status_code == 200, (r.status_code, r.text)

    # Resubmit faces — this must not fail with FK violation
    emb2 = (rng.standard_normal(512)).tolist()
    new_ids = plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
            "detection_confidence": 0.98,
            "embedding": emb2,
        },
    ])
    # The new detection doesn't re-find the confirmed face (no overlap, a
    # different embedding), so that face is kept: re-detection never drops
    # human data (ADR-016).
    assert face_count(auth_client, asset_id) == 2
    assert face_id not in new_ids
    r = auth_client.get(f"/v1/assets/{asset_id}/faces")
    assert face_id in {f["face_id"] for f in r.json()["faces"]}


@pytest.mark.slow
def test_cleanup_empty_dismissed(face_client: Tuple[_AuthClient, str, str]) -> None:
    """Dismissed people with zero face matches are deleted by cleanup.

    Re-detection no longer empties a dismissed person (its matches are
    confirmed and survive), so the person is emptied by un-assigning.
    """
    import numpy as np
    from src.server.repository.tenant import PersonRepository

    auth_client, library_id, tenant_url = face_client
    asset_id = _create_asset(tenant_url, library_id, "face_cleanup_dismissed")

    # Use random embedding to avoid auto-assignment collisions
    rng = np.random.default_rng(99)
    emb1 = (rng.standard_normal(512)).tolist()

    # Submit a face and assign it to a person
    [face_id] = plant_faces(auth_client, asset_id, [
        {
            "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3},
            "detection_confidence": 0.95,
            "embedding": emb1,
        },
    ])

    r = auth_client.post(
        f"/v1/faces/{face_id}/assign",
        json={"new_person_name": "Dismissed Cleanup Test"},
    )
    assert r.status_code == 200
    person_id = r.json()["person_id"]

    # Mark person as dismissed directly in DB
    engine = create_engine(tenant_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE people SET dismissed = true, display_name = '(dismissed)' WHERE person_id = :pid"),
                     {"pid": person_id})
    engine.dispose()

    # Remove the person's only face, leaving the person empty
    r = auth_client.delete(f"/v1/faces/{face_id}/assign")
    assert r.status_code == 204

    # Run cleanup directly via repository (avoids multi-tenant routing issues in test)
    from sqlmodel import Session as SmSession
    test_engine = create_engine(tenant_url)
    with SmSession(test_engine) as session:
        deleted = PersonRepository(session).cleanup_empty_dismissed()
        assert deleted >= 1
    test_engine.dispose()

    # Person should be gone
    r = auth_client.get(f"/v1/people/{person_id}")
    assert r.status_code == 404
