"""Slow tests for trash-aware face/people queries.

After the user reported 404s when opening a named person's photos in
the lightbox, an audit found seven query paths that didn't filter
``assets.deleted_at IS NULL``:

- ``PersonRepository.get_faces``           (the headline 404 — covered)
- ``PersonRepository.get_face_count``      (covered)
- ``PersonRepository.list_with_face_counts`` (covered)
- ``PersonRepository.list_dismissed``      (symmetric — not separately covered;
                                            shares the same SQL pattern as
                                            ``list_with_face_counts``)
- ``PersonRepository._recompute_centroid`` (covered indirectly via the
                                            upgrade-step test)
- ``FaceRepository.compute_clusters``      (covered)
- ``FaceRepository.propagate_assignments`` (covered)

Each test creates two assets — one active, one trashed — both with
faces, then verifies that the relevant query path returns only the
active-asset face. The face_person_matches rows themselves are kept
intact across all paths (the queries hide them rather than delete
them) so untrashing the asset restores the assignment automatically.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from testcontainers.postgres import PostgresContainer
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.server.repository.system_metadata import SystemMetadataRepository
from src.server.repository.tenant import (
    FaceRepository,
    PersonRepository,
)
from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.steps.recompute_centroids_for_trash_filter import (
    RecomputeCentroidsForTrashFilterStep,
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


@pytest.fixture
def session(tenant_engine):
    """Per-test session that wipes the relevant tables on entry.

    The module-scoped engine is shared across tests, so we can't rely
    on container teardown for isolation. Truncating the per-test data
    is cheap and keeps each test independent.
    """
    with Session(tenant_engine) as session:
        # Order matters — face_person_matches → faces → assets → libraries
        # because of FK chains. people.representative_face_id is nullable
        # so we don't need to null it before truncating faces.
        for table in (
            "face_person_matches",
            "faces",
            "people",
            "asset_metadata",
            "asset_embeddings",
            "video_scenes",
            "video_index_chunks",
            "collection_assets",
            "asset_ratings",
            "assets",
            "libraries",
        ):
            session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        session.commit()
        yield session


# ---------------------------------------------------------------------------
# Helpers — direct SQL inserts so the tests don't depend on the upsert API.
# ---------------------------------------------------------------------------


def _new_library(session: Session) -> str:
    library_id = "lib_" + uuid.uuid4().hex[:16]
    session.execute(
        text(
            "INSERT INTO libraries (library_id, name, root_path, status, created_at, updated_at)"
            " VALUES (:id, :name, '/x', 'active', NOW(), NOW())"
        ),
        {"id": library_id, "name": "TrashFilter_" + secrets.token_urlsafe(4)},
    )
    return library_id


def _new_asset(session: Session, library_id: str, name: str, *, soft_deleted: bool = False) -> str:
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


def _new_face(session: Session, asset_id: str, *, embedding: list[float] | None = None) -> str:
    face_id = "face_" + uuid.uuid4().hex[:20]
    if embedding is not None:
        emb_str = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"
        session.execute(
            text(
                "INSERT INTO faces ("
                "  face_id, asset_id, detection_confidence, detection_model,"
                "  detection_model_version, embedding_vector, created_at"
                ") VALUES (:fid, :aid, 0.9, 'insightface', 'buffalo_l',"
                "          CAST(:emb AS vector), NOW())"
            ),
            {"fid": face_id, "aid": asset_id, "emb": emb_str},
        )
    else:
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


def _new_person(session: Session, *, dismissed: bool = False) -> str:
    person_id = "person_" + uuid.uuid4().hex[:16]
    session.execute(
        text(
            "INSERT INTO people ("
            "  person_id, display_name, created_by_user, dismissed,"
            "  confirmation_count, created_at"
            ") VALUES (:pid, 'Test', true, :dis, 0, NOW())"
        ),
        {"pid": person_id, "dis": dismissed},
    )
    return person_id


def _assign(session: Session, face_id: str, person_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO face_person_matches (match_id, face_id, person_id, confidence, confirmed, created_at)"
            " VALUES (:mid, :fid, :pid, 0.9, true, NOW())"
        ),
        {"mid": "fpm_" + uuid.uuid4().hex[:16], "fid": face_id, "pid": person_id},
    )


def _unit(v: list[float]) -> list[float]:
    arr = np.array(v, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return (arr / n).tolist() if n > 0 else v


def _orthogonal_512(seed: int) -> list[float]:
    """A unit-length 512-d vector with one-hot at index ``seed``.

    The repository expects 512-d embeddings (the column type). Using
    different one-hot indices makes any two vectors orthogonal — useful
    for unambiguous clustering / propagation tests.
    """
    v = [0.0] * 512
    v[seed % 512] = 1.0
    return v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_get_faces_hides_trashed_asset_faces(session) -> None:
    """The headline 404: opening a person should not return trashed-asset faces."""
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    active_face = _new_face(session, active_aid)
    trashed_face = _new_face(session, trashed_aid)

    person_id = _new_person(session)
    _assign(session, active_face, person_id)
    _assign(session, trashed_face, person_id)
    session.commit()

    repo = PersonRepository(session)
    faces = repo.get_faces(person_id)
    face_ids = [f.face_id for f in faces]
    assert face_ids == [active_face]

    # And the match row stays — untrashing should restore visibility.
    match_count = session.execute(
        text("SELECT COUNT(*) FROM face_person_matches WHERE person_id = :pid"),
        {"pid": person_id},
    ).scalar()
    assert match_count == 2


@pytest.mark.slow
def test_get_face_count_excludes_trashed(session) -> None:
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    person_id = _new_person(session)
    _assign(session, _new_face(session, active_aid), person_id)
    _assign(session, _new_face(session, trashed_aid), person_id)
    _assign(session, _new_face(session, trashed_aid), person_id)
    session.commit()

    repo = PersonRepository(session)
    assert repo.get_face_count(person_id) == 1


@pytest.mark.slow
def test_list_with_face_counts_excludes_trashed(session) -> None:
    """A person with N faces (M trashed) should show count = N - M."""
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    person_id = _new_person(session)
    _assign(session, _new_face(session, active_aid), person_id)
    _assign(session, _new_face(session, active_aid), person_id)
    _assign(session, _new_face(session, trashed_aid), person_id)
    session.commit()

    repo = PersonRepository(session)
    rows = repo.list_with_face_counts()
    assert len(rows) == 1
    person, count = rows[0]
    assert person.person_id == person_id
    assert count == 2  # 3 matches, but 1 is on a trashed asset


@pytest.mark.slow
def test_list_with_face_counts_includes_people_with_zero_visible_faces(session) -> None:
    """A person whose only photo is in the trash should still appear with count 0."""
    library_id = _new_library(session)
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    person_id = _new_person(session)
    _assign(session, _new_face(session, trashed_aid), person_id)
    session.commit()

    repo = PersonRepository(session)
    rows = repo.list_with_face_counts()
    assert len(rows) == 1
    assert rows[0][1] == 0


@pytest.mark.slow
def test_compute_clusters_excludes_trashed_face_embeddings(session) -> None:
    """The cluster review pool must not include faces from trashed assets."""
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    # Three active faces + three trashed faces, all unassigned.
    # Use the same one-hot embedding for all active faces (they should
    # cluster together) and a different one for trashed (would form a
    # second cluster if not filtered).
    for _ in range(3):
        _new_face(session, active_aid, embedding=_orthogonal_512(0))
    for _ in range(3):
        _new_face(session, trashed_aid, embedding=_orthogonal_512(1))
    session.commit()

    repo = FaceRepository(session)
    clusters_raw, all_face_ids, _ = repo.compute_clusters(min_cluster_size=3)

    # If the trashed embeddings leaked through, we'd see 2 clusters of
    # 3. Filter applied → only the active blob.
    assert len(clusters_raw) == 1
    assert len(all_face_ids[0]) == 3


@pytest.mark.slow
def test_propagate_assignments_skips_trashed_asset_faces(session) -> None:
    """Auto-propagation should never touch faces on trashed assets."""
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    # A named person with an active-face exemplar so the centroid is
    # well-defined and within AUTO_ASSIGN_THRESHOLD of the candidates.
    person_id = _new_person(session)
    seed = _new_face(session, active_aid, embedding=_orthogonal_512(0))
    _assign(session, seed, person_id)

    # Recompute centroid via the public path so we know it's correct.
    PersonRepository(session)._recompute_centroid(person_id)
    session.commit()

    # An unassigned active face that should be picked up.
    target_active = _new_face(session, active_aid, embedding=_orthogonal_512(0))
    # And an unassigned trashed face that should NOT be picked up,
    # even though its embedding is identical and well within threshold.
    target_trashed = _new_face(session, trashed_aid, embedding=_orthogonal_512(0))
    session.commit()

    result = FaceRepository(session).propagate_assignments()
    assert result["assigned"] == 1, result

    # The active one got the match.
    assigned = session.execute(
        text("SELECT face_id FROM face_person_matches WHERE person_id = :pid"),
        {"pid": person_id},
    ).fetchall()
    assigned_face_ids = {r[0] for r in assigned}
    assert target_active in assigned_face_ids
    assert target_trashed not in assigned_face_ids


@pytest.mark.slow
def test_recompute_centroids_upgrade_step_idempotent(session) -> None:
    """The upgrade-step recomputes centroids and is a no-op on a clean DB."""
    library_id = _new_library(session)
    active_aid = _new_asset(session, library_id, "active")
    trashed_aid = _new_asset(session, library_id, "trashed", soft_deleted=True)

    person_id = _new_person(session)
    _assign(session, _new_face(session, active_aid, embedding=_orthogonal_512(0)), person_id)
    _assign(session, _new_face(session, trashed_aid, embedding=_orthogonal_512(0)), person_id)
    session.commit()

    ctx = UpgradeContext(session=session, metadata=SystemMetadataRepository(session))
    step = RecomputeCentroidsForTrashFilterStep()

    assert step.needs_work(ctx) is True
    result = step.run(ctx)
    assert result["recomputed"] == 1

    # And the cluster cache is now dirty.
    flag = session.execute(
        text("SELECT value FROM system_metadata WHERE key = 'face_clusters_dirty'")
    ).scalar()
    assert flag == "true"

    # Untrash to make a second run a no-op.
    session.execute(
        text("UPDATE assets SET deleted_at = NULL WHERE asset_id = :id"),
        {"id": trashed_aid},
    )
    session.commit()
    assert step.needs_work(ctx) is False
