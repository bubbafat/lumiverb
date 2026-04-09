"""Slow tests for the redetection → representative_face_id healing path.

When the macOS / CLI face provider re-runs detection on an asset that
already has named-person faces, ``FaceRepository.submit_faces`` deletes
all old face rows and inserts new ones with new ULIDs. The previous
representative face for any affected person is therefore orphaned. This
file pins the two healing mechanisms:

1. **Eager**: ``submit_faces`` re-picks a representative for every
   affected person from its current ``face_person_matches`` rows
   *inside the same transaction*. Verified by re-detecting and then
   reading the person row directly — the representative must be a
   freshly-created face_id, not the old one.

2. **Lazy**: ``GET /v1/people`` backfills ``representative_face_id``
   for any person that ended up NULL. Verified by manually nulling
   the column (simulating a pre-fix tenant) and then hitting the list
   endpoint — the field comes back populated.
"""

from __future__ import annotations

import os
import secrets
import uuid
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
from tests.conftest import (
    _AuthClient,
    _ensure_psycopg2,
    _provision_tenant_db,
    _run_control_migrations,
)


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
                    json={"name": "RedetectRepTenant", "plan": "free"},
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
                lib_name = "RedetectRepLib_" + secrets.token_urlsafe(4)
                r_lib = auth_client.post(
                    "/v1/libraries",
                    json={"name": lib_name, "root_path": "/faces"},
                )
                assert r_lib.status_code == 200, (r_lib.status_code, r_lib.text)
                library_id = r_lib.json()["library_id"]

                yield auth_client, library_id, tenant_url

        _engines.clear()


def _orthogonal_512(seed: int) -> list[float]:
    v = [0.0] * 512
    v[seed % 512] = 1.0
    return v


def _emb_to_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


def _seed_named_person_with_face(
    tenant_url: str, library_id: str, *, idx: int, name: str
) -> tuple[str, str, str]:
    """Insert a single asset + a named person with one confirmed face.

    Returns ``(asset_id, person_id, face_id)``. The face's embedding is
    a unit vector at ``idx`` so the person's centroid lines up exactly
    with embeddings the test re-submits later.
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
                    "  :id, :lib, :rp, 1000, NOW(), 'image', 'online',"
                    "  'discovered', NOW(), NOW())"
                ),
                {"id": asset_id, "lib": library_id, "rp": f"x/{name}.jpg"},
            )

            person_id = "person_" + uuid.uuid4().hex[:16]
            face_id = "face_" + uuid.uuid4().hex[:20]

            session.execute(
                text(
                    "INSERT INTO faces ("
                    "  face_id, asset_id, detection_confidence, detection_model,"
                    "  detection_model_version, embedding_vector, created_at"
                    ") VALUES (:fid, :aid, 0.95, 'insightface', 'buffalo_l',"
                    "          CAST(:v AS vector), NOW())"
                ),
                {
                    "fid": face_id,
                    "aid": asset_id,
                    "v": _emb_to_pgvector(_orthogonal_512(idx)),
                },
            )
            session.execute(
                text(
                    "INSERT INTO people ("
                    "  person_id, display_name, created_by_user, dismissed,"
                    "  centroid_vector, confirmation_count, representative_face_id,"
                    "  created_at"
                    ") VALUES (:pid, :name, true, false,"
                    "          CAST(:v AS vector), 1, :fid, NOW())"
                ),
                {
                    "pid": person_id,
                    "name": name,
                    "v": _emb_to_pgvector(_orthogonal_512(idx)),
                    "fid": face_id,
                },
            )
            session.execute(
                text(
                    "INSERT INTO face_person_matches ("
                    "  match_id, face_id, person_id, confidence, confirmed, created_at)"
                    " VALUES (:mid, :fid, :pid, 1.0, true, NOW())"
                ),
                {
                    "mid": "fpm_" + uuid.uuid4().hex[:16],
                    "fid": face_id,
                    "pid": person_id,
                },
            )
            session.commit()
            return asset_id, person_id, face_id
    finally:
        engine.dispose()


def _read_representative(tenant_url: str, person_id: str) -> str | None:
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT representative_face_id FROM people"
                    " WHERE person_id = :pid"
                ),
                {"pid": person_id},
            ).first()
            return row[0] if row else None
    finally:
        engine.dispose()


@pytest.mark.slow
def test_redetection_repicks_representative_face(people_client) -> None:
    """submit_faces should leave each affected person with a current
    representative_face_id pointing to one of the freshly-inserted
    faces — never NULL, never the orphaned old id."""
    auth_client, library_id, tenant_url = people_client
    asset_id, person_id, old_face_id = _seed_named_person_with_face(
        tenant_url, library_id, idx=11, name="RepRepick"
    )

    assert _read_representative(tenant_url, person_id) == old_face_id

    # Re-submit face detection for the same asset with the same
    # embedding so the auto-assign-by-centroid path matches.
    r = auth_client.post(
        f"/v1/assets/{asset_id}/faces",
        json={
            "detection_model": "apple_vision",
            "detection_model_version": "1",
            "faces": [
                {
                    "bounding_box": {"x1": 0.1, "y1": 0.1, "x2": 0.4, "y2": 0.4},
                    "detection_confidence": 0.92,
                    "embedding": _orthogonal_512(11),
                }
            ],
        },
    )
    assert r.status_code == 201, (r.status_code, r.text)

    new_rep = _read_representative(tenant_url, person_id)
    assert new_rep is not None, "redetection left representative_face_id NULL"
    assert new_rep != old_face_id, "redetection failed to refresh representative"
    # And the new rep must really exist in faces
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            row = session.execute(
                text("SELECT face_id, asset_id FROM faces WHERE face_id = :fid"),
                {"fid": new_rep},
            ).first()
            assert row is not None, "new representative points at a missing face row"
            assert row[1] == asset_id
    finally:
        engine.dispose()


@pytest.mark.slow
def test_list_people_lazy_backfills_null_representative(people_client) -> None:
    """GET /v1/people should self-heal an orphaned representative_face_id
    so pre-fix tenants don't see a grid full of blank tiles. Simulates
    the dangling state by manually NULLing the column."""
    auth_client, library_id, tenant_url = people_client
    _asset_id, person_id, face_id = _seed_named_person_with_face(
        tenant_url, library_id, idx=37, name="RepLazyBackfill"
    )

    # Simulate the pre-fix dangling state.
    engine = create_engine(tenant_url, future=True)
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    "UPDATE people SET representative_face_id = NULL"
                    " WHERE person_id = :pid"
                ),
                {"pid": person_id},
            )
            session.commit()
    finally:
        engine.dispose()
    assert _read_representative(tenant_url, person_id) is None

    r = auth_client.get("/v1/people", params={"limit": 100})
    assert r.status_code == 200, (r.status_code, r.text)
    items = r.json()["items"]
    target = next((p for p in items if p["person_id"] == person_id), None)
    assert target is not None, "seeded person missing from list response"
    assert target["representative_face_id"] == face_id, (
        "list_people did not lazily backfill representative_face_id"
    )

    # And the row was actually persisted, not just patched on the response.
    assert _read_representative(tenant_url, person_id) == face_id
