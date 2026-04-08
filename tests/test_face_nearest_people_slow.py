"""Slow test for ``GET /v1/faces/{face_id}/nearest-people``.

Verifies the new endpoint that the lightbox face-assignment popover
uses to rank candidate people by similarity to a single clicked face's
embedding (instead of by total face count, which has nothing to do
with whether they're the right person).

The test sets up two named people with deliberately orthogonal
centroids, drops an unassigned face whose embedding matches one of
them exactly, and asserts the matching person ranks first with
distance ~0 while the other ranks second with distance ~1.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer
from sqlalchemy import create_engine, text
from sqlmodel import Session
from unittest.mock import patch

from src.server.api.main import app
from src.server.config import get_settings
from src.server.database import _engines, get_control_session
from src.server.repository.control_plane import TenantDbRoutingRepository
from src.server.repository.tenant import PersonRepository
from tests.conftest import _AuthClient, _ensure_psycopg2, _provision_tenant_db, _run_control_migrations


@pytest.fixture(scope="module")
def people_client() -> Tuple[_AuthClient, str, str]:
    """Provision control + tenant DBs and return an authenticated client."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with PostgresContainer("pgvector/pgvector:pg16") as control_postgres:
        control_url = _ensure_psycopg2(control_postgres.get_connection_url())
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
                    json={"name": "FaceNearestTenant", "plan": "free"},
                    headers={"Authorization": "Bearer test-admin-secret"},
                )
                assert r.status_code == 200, (r.status_code, r.text)
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
                lib_name = "FaceNearestLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/faces"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _orthogonal_512(seed: int) -> list[float]:
    """Unit vector with a single one at index ``seed``."""
    v = [0.0] * 512
    v[seed % 512] = 1.0
    return v


def _emb_to_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def _seed_data(tenant_url: str, library_id: str) -> tuple[str, str, str]:
    """Insert two named people with orthogonal centroids and one
    unassigned face whose embedding matches person A exactly.

    Returns (face_id, person_a_id, person_b_id).
    """
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            asset_id = "ast_" + uuid.uuid4().hex[:20]
            session.execute(
                text(
                    "INSERT INTO assets ("
                    "  asset_id, library_id, rel_path, file_size, file_mtime,"
                    "  media_type, availability, status, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :lib, 'x/seed.jpg', 1000, NOW(), 'image', 'online',"
                    "  'discovered', NOW(), NOW())"
                ),
                {"id": asset_id, "lib": library_id},
            )

            # Person A — centroid at index 0
            person_a = "person_" + uuid.uuid4().hex[:16]
            person_b = "person_" + uuid.uuid4().hex[:16]
            for pid, idx in ((person_a, 0), (person_b, 5)):
                session.execute(
                    text(
                        "INSERT INTO people ("
                        "  person_id, display_name, created_by_user, dismissed,"
                        "  centroid_vector, confirmation_count, created_at"
                        ") VALUES (:pid, :name, true, false,"
                        "          CAST(:v AS vector), 0, NOW())"
                    ),
                    {
                        "pid": pid,
                        "name": "PersonA" if idx == 0 else "PersonB",
                        "v": _emb_to_pgvector(_orthogonal_512(idx)),
                    },
                )
                # Each person needs at least one match for the face_count
                # column in the response to be > 0.
                fid_seed = "face_" + uuid.uuid4().hex[:20]
                session.execute(
                    text(
                        "INSERT INTO faces ("
                        "  face_id, asset_id, detection_confidence, detection_model,"
                        "  detection_model_version, embedding_vector, person_id, created_at"
                        ") VALUES (:fid, :aid, 0.9, 'insightface', 'buffalo_l',"
                        "          CAST(:v AS vector), :pid, NOW())"
                    ),
                    {
                        "fid": fid_seed,
                        "aid": asset_id,
                        "v": _emb_to_pgvector(_orthogonal_512(idx)),
                        "pid": pid,
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO face_person_matches (match_id, face_id, person_id, confidence, confirmed, created_at)"
                        " VALUES (:mid, :fid, :pid, 1.0, true, NOW())"
                    ),
                    {
                        "mid": "fpm_" + uuid.uuid4().hex[:16],
                        "fid": fid_seed,
                        "pid": pid,
                    },
                )

            # The unassigned face we'll query — embedding identical to PersonA
            target_face = "face_" + uuid.uuid4().hex[:20]
            session.execute(
                text(
                    "INSERT INTO faces ("
                    "  face_id, asset_id, detection_confidence, detection_model,"
                    "  detection_model_version, embedding_vector, created_at"
                    ") VALUES (:fid, :aid, 0.95, 'insightface', 'buffalo_l',"
                    "          CAST(:v AS vector), NOW())"
                ),
                {
                    "fid": target_face,
                    "aid": asset_id,
                    "v": _emb_to_pgvector(_orthogonal_512(0)),
                },
            )
            session.commit()
            return target_face, person_a, person_b
    finally:
        engine.dispose()


@pytest.mark.slow
def test_nearest_people_for_face_ranks_by_similarity(people_client) -> None:
    auth_client, library_id, tenant_url = people_client
    face_id, person_a, person_b = _seed_data(tenant_url, library_id)

    r = auth_client.get(f"/v1/faces/{face_id}/nearest-people", params={"limit": 5})
    assert r.status_code == 200, (r.status_code, r.text)
    data = r.json()
    assert len(data) == 2, data

    # PersonA's centroid matches the face's embedding exactly → distance ~0.
    # PersonB's centroid is orthogonal → distance ~1. The list is sorted
    # ascending by distance, so PersonA must come first.
    assert data[0]["person_id"] == person_a, data
    assert data[0]["distance"] < 0.01
    assert data[1]["person_id"] == person_b
    assert data[1]["distance"] > 0.99


@pytest.mark.slow
def test_nearest_people_for_face_404_when_face_missing(people_client) -> None:
    auth_client, _library_id, _tenant_url = people_client
    r = auth_client.get("/v1/faces/face_does_not_exist/nearest-people")
    assert r.status_code == 404


@pytest.mark.slow
def test_nearest_people_for_face_empty_when_face_has_no_embedding(people_client) -> None:
    """A face row that exists but has no embedding returns [], not 404.

    This is the documented behavior: the lightbox popover should fall
    back to the alphabetical list rather than show an error path.
    """
    auth_client, library_id, tenant_url = people_client

    # Insert a face with NULL embedding
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            asset_id = "ast_" + uuid.uuid4().hex[:20]
            session.execute(
                text(
                    "INSERT INTO assets ("
                    "  asset_id, library_id, rel_path, file_size, file_mtime,"
                    "  media_type, availability, status, created_at, updated_at"
                    ") VALUES ("
                    "  :id, :lib, 'x/no-emb.jpg', 1000, NOW(), 'image',"
                    "  'online', 'discovered', NOW(), NOW())"
                ),
                {"id": asset_id, "lib": library_id},
            )
            face_id = "face_" + uuid.uuid4().hex[:20]
            session.execute(
                text(
                    "INSERT INTO faces ("
                    "  face_id, asset_id, detection_confidence, detection_model,"
                    "  detection_model_version, created_at"
                    ") VALUES (:fid, :aid, 0.9, 'insightface', 'buffalo_l', NOW())"
                ),
                {"fid": face_id, "aid": asset_id},
            )
            session.commit()
    finally:
        engine.dispose()

    r = auth_client.get(f"/v1/faces/{face_id}/nearest-people")
    assert r.status_code == 200
    assert r.json() == []
