"""Slow tests for the orphan-asset-child cleanup path.

Covers two related fixes that ship together:

1. ``AssetRepository.permanently_delete`` now cleans up every child
   table that holds an ``asset_id`` FK. The original bug missed
   ``faces`` (among others), leaving orphan rows whose dead asset_id
   broke the lightbox after a hard delete.

2. ``CleanupOrphanAssetChildrenStep`` removes any existing residue from
   the buggy delete path on every tenant. The step is idempotent so it
   safely runs on every server boot.

These tests use a real tenant Postgres (testcontainers) because the
correctness of the SQL — FK ordering, NOT EXISTS subqueries, the
``representative_face_id`` SET NULL — only manifests against a live
schema. A purely mocked unit test would not have caught the production
bug.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timezone

import pytest
from testcontainers.postgres import PostgresContainer
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.server.repository.system_metadata import SystemMetadataRepository
from src.server.repository.tenant import AssetRepository
from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.steps.cleanup_orphan_asset_children import (
    CleanupOrphanAssetChildrenStep,
)
from tests.conftest import _ensure_psycopg2, _provision_tenant_db


@pytest.fixture(scope="module")
def tenant_engine():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        url = _ensure_psycopg2(pg.get_connection_url())
        _provision_tenant_db(url, project_root)
        engine = create_engine(url, future=True)
        try:
            yield engine
        finally:
            engine.dispose()


def _new_asset(session: Session, library_id: str, name: str, *, soft_deleted: bool = False) -> str:
    """Insert one asset row directly via SQL and return its id.

    Avoids going through the API so the test can isolate the delete-path
    behavior from upsert/scan logic.
    """
    asset_id = "ast_" + uuid.uuid4().hex[:20]
    deleted_at = datetime.now(timezone.utc) if soft_deleted else None
    session.execute(
        text(
            "INSERT INTO assets ("
            "  asset_id, library_id, rel_path, file_size, file_mtime,"
            "  media_type, availability, status, deleted_at,"
            "  created_at, updated_at"
            ") VALUES ("
            "  :id, :lib, :rel, 1000, NOW(), 'image', 'online', 'discovered',"
            "  :del, NOW(), NOW()"
            ")"
        ),
        {"id": asset_id, "lib": library_id, "rel": f"x/{name}.jpg", "del": deleted_at},
    )
    return asset_id


def _new_library(session: Session) -> str:
    library_id = "lib_" + uuid.uuid4().hex[:16]
    session.execute(
        text(
            "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
            " VALUES (:id, :name, '/x', 'active', NOW(), NOW())"
        ),
        {"id": library_id, "name": "OrphanTestLib_" + secrets.token_urlsafe(4)},
    )
    return library_id


def _insert_face(session: Session, asset_id: str) -> str:
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
    return face_id


def _insert_person_with_rep_face(session: Session, face_id: str) -> str:
    person_id = "person_" + uuid.uuid4().hex[:16]
    session.execute(
        text(
            "INSERT INTO people ("
            "  person_id, display_name, created_by_user, dismissed,"
            "  representative_face_id, confirmation_count, created_at"
            ") VALUES (:pid, 'Test', true, false, :fid, 0, NOW())"
        ),
        {"pid": person_id, "fid": face_id},
    )
    return person_id


def _insert_face_person_match(session: Session, face_id: str, person_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO face_person_matches (match_id, face_id, person_id, confidence, confirmed, created_at)"
            " VALUES (:mid, :fid, :pid, 0.95, true, NOW())"
        ),
        {"mid": "fpm_" + uuid.uuid4().hex[:16], "fid": face_id, "pid": person_id},
    )


@pytest.mark.slow
def test_permanently_delete_cleans_up_face_rows(tenant_engine) -> None:
    """The original 404 bug: hard-deleting an asset must take its faces."""
    with Session(tenant_engine) as session:
        library_id = _new_library(session)
        asset_id = _new_asset(session, library_id, "delme", soft_deleted=True)
        face_id = _insert_face(session, asset_id)
        person_id = _insert_person_with_rep_face(session, face_id)
        _insert_face_person_match(session, face_id, person_id)
        session.commit()

        repo = AssetRepository(session)
        deleted = repo.permanently_delete([asset_id])
        assert deleted == 1

        # Asset gone.
        assert session.execute(
            text("SELECT 1 FROM assets WHERE asset_id = :id"), {"id": asset_id}
        ).first() is None
        # Face gone — this is the regression check.
        assert session.execute(
            text("SELECT 1 FROM faces WHERE face_id = :id"), {"id": face_id}
        ).first() is None
        # Match gone (cleared before face).
        assert session.execute(
            text("SELECT 1 FROM face_person_matches WHERE face_id = :id"),
            {"id": face_id},
        ).first() is None
        # People row survives but the FK is nulled out.
        rep = session.execute(
            text("SELECT representative_face_id FROM people WHERE person_id = :id"),
            {"id": person_id},
        ).scalar()
        assert rep is None


@pytest.mark.slow
def test_cleanup_orphan_step_removes_pre_existing_orphans(tenant_engine) -> None:
    """The upgrade step recovers from past invocations of the buggy delete path.

    Simulates the production residue: a face row whose ``asset_id`` was
    never in the assets table (because the buggy permanently_delete
    removed the asset but left the face). The step must delete the face
    + its face_person_matches and null any people pointing at it.
    """
    with Session(tenant_engine) as session:
        # Insert an orphan face referencing a non-existent asset id.
        # We have to bypass the FK by temporarily disabling it for this row,
        # because the schema does have the FK declared (just no CASCADE).
        # Use SET session_replication_role = 'replica' which suppresses
        # FK trigger checks for inserts in this session only.
        session.execute(text("SET session_replication_role = 'replica'"))
        ghost_asset_id = "ast_" + uuid.uuid4().hex[:20]  # never created
        face_id = _insert_face(session, ghost_asset_id)
        person_id = _insert_person_with_rep_face(session, face_id)
        _insert_face_person_match(session, face_id, person_id)
        session.execute(text("SET session_replication_role = 'origin'"))
        session.commit()

        ctx = UpgradeContext(
            session=session,
            metadata=SystemMetadataRepository(session),
        )
        step = CleanupOrphanAssetChildrenStep()

        assert step.needs_work(ctx) is True
        result = step.run(ctx)
        assert result["faces"] == 1, result

        # Orphan face is gone.
        assert session.execute(
            text("SELECT 1 FROM faces WHERE face_id = :id"), {"id": face_id}
        ).first() is None
        # face_person_matches cleared.
        assert session.execute(
            text("SELECT 1 FROM face_person_matches WHERE face_id = :id"),
            {"id": face_id},
        ).first() is None
        # Representative face nulled on the surviving person.
        rep = session.execute(
            text("SELECT representative_face_id FROM people WHERE person_id = :id"),
            {"id": person_id},
        ).scalar()
        assert rep is None

        # Idempotent: a second run finds nothing.
        assert step.needs_work(ctx) is False
        second = step.run(ctx)
        assert second["faces"] == 0
