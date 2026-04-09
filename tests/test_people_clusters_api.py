"""API tests for the people router's cluster + nearest-people surfaces.

These exercise the largest uncovered code in
``src/server/api/routers/people.py``: cluster naming, cluster dismissal,
nearest-people ranking, dismissed-people listing/undismissing, and the
single-face crop endpoint.

A dedicated module-scoped fixture provisions a fresh tenant DB so the
cluster cache is computed against a known-good corpus of unassigned faces
that the test owns. The existing ``test_people_api_slow.py`` fixture
deliberately consumes its own faces during the assignment tests, so reusing
it here would leave nothing to cluster.
"""

from __future__ import annotations

import os
import secrets
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from tests.conftest import (
    _AuthClient,
    _ensure_psycopg2,
    _provision_tenant_db,
    _run_control_migrations,
)


def _seeded_embedding(label: str, dim: int = 512) -> list[float]:
    """Deterministic, well-separated embedding seeded by label."""
    import hashlib
    import random as _random

    seed = int(hashlib.sha256(label.encode()).hexdigest()[:8], 16)
    rng = _random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


def _create_asset_with_faces(
    auth_client: _AuthClient,
    library_id: str,
    name: str,
    embedding_label: str,
    n_faces: int = 1,
) -> tuple[str, list[str]]:
    """Create an asset and submit n faces with embeddings derived from
    embedding_label so groups of related faces actually cluster together.
    """
    rel_path = f"clusters/{name}.jpg"
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
    asset_id = auth_client.get(
        "/v1/assets/by-path",
        params={"library_id": library_id, "rel_path": rel_path},
    ).json()["asset_id"]

    base = _seeded_embedding(embedding_label)
    faces = []
    for i in range(n_faces):
        # Add a tiny per-face perturbation so embeddings aren't byte-identical
        # but still cluster around the same centroid.
        perturbed = [v + (i * 0.001) for v in base]
        faces.append(
            {
                "bounding_box": {"x": 0.1 * i, "y": 0.1, "w": 0.1, "h": 0.15},
                "detection_confidence": 0.95 - 0.01 * i,
                "embedding": perturbed,
            }
        )
    r3 = auth_client.post(f"/v1/assets/{asset_id}/faces", json={"faces": faces})
    assert r3.status_code == 201, r3.text
    return asset_id, r3.json()["face_ids"]


@pytest.fixture(scope="module")
def clusters_client() -> Iterator[tuple[_AuthClient, str]]:
    """Provision a fresh tenant + library and seed three groups of faces
    that should naturally form three clusters."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
        _run_control_migrations(control_url)

        u = make_url(control_url)
        os.environ["CONTROL_PLANE_DATABASE_URL"] = control_url
        os.environ["TENANT_DATABASE_URL_TEMPLATE"] = str(u.set(database="{tenant_id}"))
        os.environ["ADMIN_KEY"] = "test-admin-secret"
        os.environ["JWT_SECRET"] = "test-jwt-secret"
        get_settings.cache_clear()
        _engines.clear()

        with patch("src.server.api.routers.admin.provision_tenant_database"):
            with TestClient(app) as client:
                r = client.post(
                    "/v1/admin/tenants",
                    json={"name": "ClusterTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200
                tenant_id = r.json()["tenant_id"]
                api_key = r.json()["api_key"]

        with PostgresContainer("pgvector/pgvector:pg16") as tenant_postgres:
            tenant_url = _ensure_psycopg2(tenant_postgres.get_connection_url())
            _provision_tenant_db(tenant_url, project_root)

            with get_control_session() as session:
                row = TenantDbRoutingRepository(session).get_by_tenant_id(tenant_id)
                assert row is not None
                row.connection_string = tenant_url
                session.add(row)
                session.commit()

            with TestClient(app) as client:
                auth_client = _AuthClient(client, api_key)
                lib_name = "ClusterLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/clusters"},
                )
                assert r_lib.status_code == 200
                library_id = r_lib.json()["library_id"]

                # Three identity groups, ~3 faces each → produces clusters
                # of size 3 from 9 unassigned faces.
                for group in ("alpha", "beta", "gamma"):
                    for i in range(3):
                        _create_asset_with_faces(
                            auth_client,
                            library_id,
                            name=f"{group}_{i}",
                            embedding_label=group,
                        )

                yield auth_client, library_id

        _engines.clear()


# ---- list_people / search / dismissed -------------------------------------


@pytest.mark.slow
def test_list_people_q_filter_returns_match(clusters_client) -> None:
    auth_client, _ = clusters_client

    # Create two named people so we can search by name
    auth_client.post("/v1/people", json={"display_name": "QueryAlice"})
    auth_client.post("/v1/people", json={"display_name": "QueryBob"})

    r = auth_client.get("/v1/people", params={"q": "QueryAli"})
    assert r.status_code == 200
    names = [p["display_name"] for p in r.json()["items"]]
    assert "QueryAlice" in names
    assert "QueryBob" not in names


@pytest.mark.slow
def test_list_people_pagination_limit_clamping(clusters_client) -> None:
    auth_client, _ = clusters_client
    # Limits below 1 are clamped to 1, above 100 to 100
    r = auth_client.get("/v1/people", params={"limit": 0})
    assert r.status_code == 200
    assert len(r.json()["items"]) <= 1

    r = auth_client.get("/v1/people", params={"limit": 999})
    assert r.status_code == 200


# ---- name_cluster + list_cluster_faces + nearest_people_for_cluster ------


@pytest.mark.slow
def test_get_clusters_returns_seeded_groups(clusters_client) -> None:
    auth_client, _ = clusters_client

    r = auth_client.get("/v1/faces/clusters")
    assert r.status_code == 200
    data = r.json()
    # Three identity groups → at least three clusters of size >= 2
    assert len(data["clusters"]) >= 3


@pytest.mark.slow
def test_list_cluster_faces_paginates(clusters_client) -> None:
    auth_client, _ = clusters_client

    clusters = auth_client.get("/v1/faces/clusters").json()["clusters"]
    assert clusters, "expected seeded clusters"
    cluster_index = clusters[0]["cluster_index"]

    r = auth_client.get(f"/v1/faces/clusters/{cluster_index}/faces", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    assert len(body["items"]) <= 2
    for item in body["items"]:
        assert "face_id" in item
        assert "asset_id" in item

    # 404 for an out-of-range cluster index
    r = auth_client.get("/v1/faces/clusters/99999/faces")
    assert r.status_code == 404


@pytest.mark.slow
def test_nearest_people_for_cluster(clusters_client) -> None:
    auth_client, _ = clusters_client

    # Need at least one named person with a centroid for ranking to return rows
    clusters = auth_client.get("/v1/faces/clusters").json()["clusters"]
    cluster_to_name = clusters[0]
    r = auth_client.post(
        f"/v1/faces/clusters/{cluster_to_name['cluster_index']}/name",
        json={"display_name": f"NameClusterTarget_{secrets.token_urlsafe(3)}"},
    )
    assert r.status_code == 201, r.text
    named_person_id = r.json()["person_id"]

    # Now query nearest people for a *different* cluster
    remaining = auth_client.get("/v1/faces/clusters").json()["clusters"]
    other = next((c for c in remaining if c["cluster_index"] != cluster_to_name["cluster_index"]), None)
    assert other is not None, "needed at least 2 clusters"

    r = auth_client.get(f"/v1/faces/clusters/{other['cluster_index']}/nearest-people")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    assert any(p["person_id"] == named_person_id for p in items)
    for p in items:
        assert 0.0 <= p["distance"] <= 2.0  # cosine distance bound

    # Out-of-range cluster → 404
    r = auth_client.get("/v1/faces/clusters/99999/nearest-people")
    assert r.status_code == 404


@pytest.mark.slow
def test_name_cluster_assigns_to_existing_person(clusters_client) -> None:
    """name_cluster with person_id assigns all cluster faces to that person."""
    auth_client, _ = clusters_client

    # Create a person to receive the cluster
    r = auth_client.post("/v1/people", json={"display_name": "ExistingTarget"})
    assert r.status_code == 201
    person_id = r.json()["person_id"]

    clusters = auth_client.get("/v1/faces/clusters").json()["clusters"]
    target_cluster = clusters[0]
    expected_size = target_cluster["size"]

    r = auth_client.post(
        f"/v1/faces/clusters/{target_cluster['cluster_index']}/name",
        json={"display_name": "ignored", "person_id": person_id},
    )
    assert r.status_code == 201
    assert r.json()["person_id"] == person_id

    # The named person should now have at least the cluster's faces
    r = auth_client.get(f"/v1/people/{person_id}/faces")
    assert r.status_code == 200
    assert len(r.json()["items"]) >= expected_size


@pytest.mark.slow
def test_name_cluster_validation_errors(clusters_client) -> None:
    auth_client, _ = clusters_client

    # Empty display_name + no person_id → 400
    r = auth_client.post(
        "/v1/faces/clusters/0/name",
        json={"display_name": "  ", "person_id": None},
    )
    assert r.status_code == 400

    # Cluster index out of range → 404
    r = auth_client.post(
        "/v1/faces/clusters/99999/name",
        json={"display_name": "Whatever"},
    )
    assert r.status_code == 404


# ---- dismiss_cluster + undismiss_person -----------------------------------


@pytest.mark.slow
def test_dismiss_then_list_then_undismiss_cluster(clusters_client) -> None:
    auth_client, _ = clusters_client

    clusters = auth_client.get("/v1/faces/clusters").json()["clusters"]
    assert clusters, "need clusters to dismiss"
    cluster_index = clusters[-1]["cluster_index"]

    r = auth_client.post(f"/v1/faces/clusters/{cluster_index}/dismiss", json={})
    assert r.status_code == 200
    dismissed_pid = r.json()["person_id"]

    # The dismissed person shows up in /v1/people/dismissed
    r = auth_client.get("/v1/people/dismissed")
    assert r.status_code == 200
    dismissed_ids = [p["person_id"] for p in r.json()["items"]]
    assert dismissed_pid in dismissed_ids

    # Now undismiss with a name
    r = auth_client.post(
        f"/v1/people/{dismissed_pid}/undismiss",
        json={"display_name": "RescuedFromDismiss"},
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "RescuedFromDismiss"

    # Dismissed list no longer contains them
    r = auth_client.get("/v1/people/dismissed")
    dismissed_ids = [p["person_id"] for p in r.json()["items"]]
    assert dismissed_pid not in dismissed_ids


@pytest.mark.slow
def test_dismiss_cluster_404_for_invalid_index(clusters_client) -> None:
    auth_client, _ = clusters_client
    r = auth_client.post("/v1/faces/clusters/99999/dismiss", json={})
    assert r.status_code == 404


@pytest.mark.slow
def test_undismiss_404_for_active_person(clusters_client) -> None:
    auth_client, _ = clusters_client
    # Create a non-dismissed person — undismiss should 404
    r = auth_client.post("/v1/people", json={"display_name": "NotDismissed"})
    assert r.status_code == 201
    pid = r.json()["person_id"]
    r = auth_client.post(f"/v1/people/{pid}/undismiss", json={"display_name": "x"})
    assert r.status_code == 404


# ---- nearest_people_for_person + nearest_people_for_face -----------------


@pytest.mark.slow
def test_nearest_people_for_person_ranks(clusters_client) -> None:
    auth_client, library_id = clusters_client

    # Build two named people with distinct centroids by creating fresh faces
    _, alpha_face_ids = _create_asset_with_faces(
        auth_client, library_id, "ranknn_alpha", "rank_alpha", n_faces=2
    )
    _, beta_face_ids = _create_asset_with_faces(
        auth_client, library_id, "ranknn_beta", "rank_beta", n_faces=2
    )

    r1 = auth_client.post("/v1/people", json={"display_name": "RankAlpha", "face_ids": alpha_face_ids})
    assert r1.status_code == 201
    alpha_pid = r1.json()["person_id"]

    r2 = auth_client.post("/v1/people", json={"display_name": "RankBeta", "face_ids": beta_face_ids})
    assert r2.status_code == 201
    beta_pid = r2.json()["person_id"]

    r = auth_client.get(f"/v1/people/{alpha_pid}/nearest")
    assert r.status_code == 200
    items = r.json()
    # Beta should appear (and not alpha itself)
    pids = [p["person_id"] for p in items]
    assert alpha_pid not in pids
    assert beta_pid in pids

    # Person with no centroid → empty list (not 404)
    r3 = auth_client.post("/v1/people", json={"display_name": "RankEmpty"})
    empty_pid = r3.json()["person_id"]
    r = auth_client.get(f"/v1/people/{empty_pid}/nearest")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.slow
def test_nearest_people_for_face(clusters_client) -> None:
    auth_client, library_id = clusters_client

    # Create a face and a separate named person to rank against
    _, query_face_ids = _create_asset_with_faces(
        auth_client, library_id, "facequery_a", "facequery_a", n_faces=1
    )
    _, target_face_ids = _create_asset_with_faces(
        auth_client, library_id, "facequery_b", "facequery_b", n_faces=2
    )
    auth_client.post(
        "/v1/people",
        json={"display_name": "FaceQueryTarget", "face_ids": target_face_ids},
    )

    r = auth_client.get(f"/v1/faces/{query_face_ids[0]}/nearest-people")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    if items:
        for p in items:
            assert "person_id" in p
            assert "distance" in p

    # Unknown face → 404
    r = auth_client.get("/v1/faces/face_nonexistent000000000000/nearest-people")
    assert r.status_code == 404


# ---- get_face_crop --------------------------------------------------------


@pytest.mark.slow
def test_get_face_crop_404_for_unknown_face(clusters_client) -> None:
    auth_client, _ = clusters_client
    r = auth_client.get("/v1/faces/face_nonexistent000000000000/crop")
    assert r.status_code == 404


@pytest.mark.slow
def test_get_face_crop_404_when_no_proxy_for_generation(clusters_client) -> None:
    """Faces submitted without a proxy on the asset have no crop_key and
    no proxy to generate from, so the endpoint must return 404."""
    auth_client, library_id = clusters_client

    _, face_ids = _create_asset_with_faces(
        auth_client, library_id, "no_proxy_face", "noproxy", n_faces=1
    )
    r = auth_client.get(f"/v1/faces/{face_ids[0]}/crop")
    assert r.status_code == 404
