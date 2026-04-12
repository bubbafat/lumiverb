"""People endpoints: CRUD for named people, face clustering."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/people", tags=["people"])


# ---------- Request / response models ----------

class PersonItem(BaseModel):
    person_id: str
    display_name: str
    face_count: int
    representative_face_id: str | None = None
    representative_asset_id: str | None = None
    confirmation_count: int = 0


class PersonListResponse(BaseModel):
    items: list[PersonItem]
    next_cursor: str | None = None


class PersonCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    face_ids: list[str] | None = Field(default=None, max_length=10_000)


class PersonUpdateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)


class FaceAssignRequest(BaseModel):
    person_id: str | None = None
    new_person_name: str | None = Field(default=None, max_length=255)


class MergeRequest(BaseModel):
    source_person_id: str


class PersonFaceItem(BaseModel):
    face_id: str
    asset_id: str
    bounding_box: dict | None = None
    detection_confidence: float | None = None
    rel_path: str | None = None
    taken_at: str | None = None  # ISO 8601, used by client-side date grouping


class PersonFacesResponse(BaseModel):
    items: list[PersonFaceItem]
    next_cursor: str | None = None


class ClusterItem(BaseModel):
    cluster_index: int
    size: int
    faces: list[dict]


class ClustersResponse(BaseModel):
    clusters: list[ClusterItem]
    truncated: bool = False
    max_cluster_size: int = 0


# ---------- Endpoints ----------

@router.get("", response_model=PersonListResponse)
def list_people(
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 50,
    q: str | None = None,
) -> PersonListResponse:
    """List people sorted by face count descending. Optional q param for name search."""
    from src.server.repository.tenant import PersonRepository, FaceRepository

    if limit > 100:
        limit = 100
    if limit < 1:
        limit = 1

    repo = PersonRepository(session)
    rows = repo.list_with_face_counts(after=after, limit=limit, q=q)

    items = []
    for person, face_count in rows:
        # Backfill representative face if missing. This heals people
        # whose previous representative was orphaned by face
        # re-detection (submit_faces deletes all old face rows for an
        # asset and inserts new ones with new ULIDs). The eager fix in
        # FaceRepository.submit_faces re-picks a representative going
        # forward; this lazy path covers people who were orphaned
        # before that fix shipped, so the user doesn't see a grid full
        # of blank tiles. Mirrors list_dismissed_people.
        if not person.representative_face_id and face_count > 0:
            from sqlalchemy import text as sa_text
            rep = session.execute(
                sa_text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST LIMIT 1"
                ),
                {"pid": person.person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep
                session.add(person)
                session.commit()

        # Get representative face's asset_id for thumbnail
        rep_asset_id = None
        if person.representative_face_id:
            from src.server.models.tenant import Face
            rep_face = session.get(Face, person.representative_face_id)
            if rep_face:
                rep_asset_id = rep_face.asset_id

        items.append(PersonItem(
            person_id=person.person_id,
            display_name=person.display_name,
            face_count=face_count,
            representative_face_id=person.representative_face_id,
            representative_asset_id=rep_asset_id,
            confirmation_count=person.confirmation_count,
        ))

    next_cursor: str | None = None
    if items and len(items) == limit:
        last = rows[-1]
        cursor_data = {"count": last[1], "id": last[0].person_id}
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(cursor_data).encode()
        ).decode().rstrip("=")

    return PersonListResponse(items=items, next_cursor=next_cursor)


@router.post("", response_model=PersonItem, status_code=201)
def create_person(
    body: PersonCreateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Create a new named person. Optionally assign face_ids."""
    from src.server.repository.tenant import PersonRepository

    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")

    repo = PersonRepository(session)

    # Check if any face_ids are already assigned
    if body.face_ids:
        from sqlalchemy import text
        already = session.execute(
            text("SELECT face_id, person_id FROM face_person_matches WHERE face_id = ANY(:fids)"),
            {"fids": body.face_ids},
        ).all()
        if already:
            conflicts = [{"face_id": r[0], "person_id": r[1]} for r in already]
            raise HTTPException(status_code=409, detail={"message": "Faces already assigned", "conflicts": conflicts})

    person = repo.create(body.display_name.strip(), face_ids=body.face_ids)
    face_count = repo.get_face_count(person.person_id)

    rep_asset_id = None
    if person.representative_face_id:
        from src.server.models.tenant import Face
        rep_face = session.get(Face, person.representative_face_id)
        if rep_face:
            rep_asset_id = rep_face.asset_id

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=person.representative_face_id,
        representative_asset_id=rep_asset_id,
        confirmation_count=person.confirmation_count,
    )


@router.get("/dismissed", response_model=PersonListResponse)
def list_dismissed_people(
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 50,
) -> PersonListResponse:
    """List dismissed people sorted by face count descending."""
    from src.server.repository.tenant import PersonRepository

    if limit > 100:
        limit = 100
    if limit < 1:
        limit = 1

    repo = PersonRepository(session)
    rows = repo.list_dismissed(after=after, limit=limit)

    items = []
    for person, face_count in rows:
        # Backfill representative face if missing (for dismissed people created before the fix)
        if not person.representative_face_id and face_count > 0:
            from sqlalchemy import text as sa_text
            rep = session.execute(
                sa_text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST LIMIT 1"
                ),
                {"pid": person.person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep
                session.add(person)
                session.commit()

        rep_asset_id = None
        if person.representative_face_id:
            from src.server.models.tenant import Face
            rep_face = session.get(Face, person.representative_face_id)
            if rep_face:
                rep_asset_id = rep_face.asset_id

        items.append(PersonItem(
            person_id=person.person_id,
            display_name=person.display_name,
            face_count=face_count,
            representative_face_id=person.representative_face_id,
            representative_asset_id=rep_asset_id,
            confirmation_count=person.confirmation_count,
        ))

    next_cursor: str | None = None
    if items and len(items) == limit:
        last = rows[-1]
        cursor_data = {"count": last[1], "id": last[0].person_id}
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(cursor_data).encode()
        ).decode().rstrip("=")

    return PersonListResponse(items=items, next_cursor=next_cursor)


class NearestPersonItem(BaseModel):
    person_id: str
    display_name: str
    face_count: int
    distance: float


@router.get("/{person_id}/nearest", response_model=list[NearestPersonItem])
def nearest_people_for_person(
    person_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = 5,
) -> list[NearestPersonItem]:
    """Return named people sorted by cosine distance to this person's centroid."""
    import numpy as np
    from sqlalchemy import text as sa_text
    from src.server.repository.tenant import PersonRepository

    if limit > 20:
        limit = 20

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None or person.centroid_vector is None:
        return []

    centroid = np.array(person.centroid_vector, dtype=np.float32)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm

    people_rows = session.execute(
        sa_text("""
            SELECT p.person_id, p.display_name, p.centroid_vector::text,
                   COUNT(m.match_id)::int AS face_count
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            WHERE p.dismissed = false AND p.centroid_vector IS NOT NULL
                  AND p.person_id != :exclude_id
            GROUP BY p.person_id
        """),
        {"exclude_id": person_id},
    ).all()

    if not people_rows:
        return []

    results = []
    for row in people_rows:
        pvec = np.array([float(x) for x in row[2].strip("[]").split(",")], dtype=np.float32)
        pnorm = np.linalg.norm(pvec)
        if pnorm > 0:
            pvec = pvec / pnorm
        dist = float(1.0 - np.dot(centroid, pvec))
        results.append(NearestPersonItem(
            person_id=row[0],
            display_name=row[1],
            face_count=row[3],
            distance=round(dist, 4),
        ))

    results.sort(key=lambda x: x.distance)
    return results[:limit]


class UndismissRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)


@router.post("/{person_id}/undismiss", response_model=PersonItem)
def undismiss_person(
    person_id: str,
    body: UndismissRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Restore a dismissed person and give them a name."""
    from src.server.repository.tenant import PersonRepository, _mark_clusters_dirty

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None or not person.dismissed:
        raise HTTPException(status_code=404, detail="Dismissed person not found")

    person.dismissed = False
    person.display_name = body.display_name.strip()
    session.add(person)

    if not person.representative_face_id:
        from sqlalchemy import text as sa_text
        rep = session.execute(
            sa_text(
                "SELECT f.face_id FROM faces f "
                "JOIN face_person_matches m ON m.face_id = f.face_id "
                "WHERE m.person_id = :pid "
                "ORDER BY f.detection_confidence DESC NULLS LAST LIMIT 1"
            ),
            {"pid": person_id},
        ).scalar()
        if rep:
            person.representative_face_id = rep

    _mark_clusters_dirty(session)
    session.commit()
    session.refresh(person)

    face_count = repo.get_face_count(person_id)
    rep_asset_id = None
    if person.representative_face_id:
        from src.server.models.tenant import Face
        rep_face = session.get(Face, person.representative_face_id)
        if rep_face:
            rep_asset_id = rep_face.asset_id

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=person.representative_face_id,
        representative_asset_id=rep_asset_id,
        confirmation_count=person.confirmation_count,
    )


@router.get("/{person_id}", response_model=PersonItem)
def get_person(
    person_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Get a person by ID."""
    from src.server.repository.tenant import PersonRepository

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    rep_asset_id = None
    if person.representative_face_id:
        from src.server.models.tenant import Face
        rep_face = session.get(Face, person.representative_face_id)
        if rep_face:
            rep_asset_id = rep_face.asset_id

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=person.representative_face_id,
        representative_asset_id=rep_asset_id,
        confirmation_count=person.confirmation_count,
    )


@router.patch("/{person_id}", response_model=PersonItem)
def update_person(
    person_id: str,
    body: PersonUpdateRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Update a person's display name."""
    from src.server.repository.tenant import PersonRepository

    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")

    repo = PersonRepository(session)
    person = repo.update_name(person_id, body.display_name.strip())
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=person.representative_face_id,
        confirmation_count=person.confirmation_count,
    )


@router.delete("/{person_id}", status_code=204)
def delete_person(
    person_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Delete a person and all their face matches."""
    from src.server.repository.tenant import PersonRepository

    repo = PersonRepository(session)
    if not repo.delete(person_id):
        raise HTTPException(status_code=404, detail="Person not found")


@router.get("/{person_id}/faces", response_model=PersonFacesResponse)
def list_person_faces(
    person_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 50,
) -> PersonFacesResponse:
    """List faces matched to a person, cursor-paginated."""
    from src.server.repository.tenant import PersonRepository

    if limit > 100:
        limit = 100

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    faces = repo.get_faces(person_id, after=after, limit=limit)

    # Get asset rel_paths for each face
    asset_ids = list({f.asset_id for f in faces})
    from src.server.models.tenant import Asset
    from sqlmodel import select
    asset_map: dict[str, tuple[str, object]] = {}
    if asset_ids:
        stmt = select(Asset.asset_id, Asset.rel_path, Asset.taken_at).where(Asset.asset_id.in_(asset_ids))
        asset_map = {r[0]: (r[1], r[2]) for r in session.exec(stmt).all()}

    items = [
        PersonFaceItem(
            face_id=f.face_id,
            asset_id=f.asset_id,
            bounding_box=f.bounding_box_json,
            detection_confidence=f.detection_confidence,
            rel_path=(asset_map.get(f.asset_id) or (None, None))[0],
            taken_at=(asset_map.get(f.asset_id) or (None, None))[1].isoformat()
                if (asset_map.get(f.asset_id) or (None, None))[1] is not None
                else None,
        )
        for f in faces
    ]

    next_cursor: str | None = None
    if items and len(items) == limit:
        next_cursor = items[-1].face_id

    return PersonFacesResponse(items=items, next_cursor=next_cursor)


@router.post("/{person_id}/merge", response_model=PersonItem)
def merge_person(
    person_id: str,
    body: MergeRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Merge source person into target (person_id). Source is deleted."""
    from src.server.repository.tenant import PersonRepository

    if body.source_person_id == person_id:
        raise HTTPException(status_code=400, detail="Cannot merge a person into themselves")

    repo = PersonRepository(session)
    target = repo.merge(person_id, body.source_person_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    rep_asset_id = None
    if target.representative_face_id:
        from src.server.models.tenant import Face
        rep_face = session.get(Face, target.representative_face_id)
        if rep_face:
            rep_asset_id = rep_face.asset_id

    return PersonItem(
        person_id=target.person_id,
        display_name=target.display_name,
        face_count=face_count,
        representative_face_id=target.representative_face_id,
        representative_asset_id=rep_asset_id,
        confirmation_count=target.confirmation_count,
    )


# ---------- Clusters endpoint (on /v1/faces prefix) ----------

faces_router = APIRouter(prefix="/v1/faces", tags=["faces"])


class ClusterNameRequest(BaseModel):
    # Either display_name (create new person) or person_id (merge into
    # existing). The handler validates exactly one is provided. Both
    # are optional at the schema level so Pydantic doesn't reject
    # merge-only requests before the handler can run.
    display_name: str | None = None
    person_id: str | None = None  # assign to existing person instead of creating new


@faces_router.post("/clusters/{cluster_index}/name", response_model=PersonItem, status_code=201)
def name_cluster(
    cluster_index: int,
    body: ClusterNameRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Name all faces in a cluster. Creates a new person or assigns to existing.

    Uses the cached cluster data — if cache is stale, recomputes first.
    """
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import PersonRepository, FaceRepository

    has_name = body.display_name is not None and body.display_name.strip() != ""
    if not has_name and not body.person_id:
        raise HTTPException(status_code=400, detail="Provide display_name or person_id")

    # Get cluster face IDs from cache — use existing cache even if dirty
    # to preserve stable indices during rapid name/dismiss operations.
    meta = SystemMetadataRepository(session)
    cached = meta.get_value("face_clusters_cache")

    if not cached:
        repo = FaceRepository(session)
        clusters_raw, all_face_ids, truncated = repo.compute_clusters(
            max_clusters=50, faces_per_cluster=20,
        )
        cache_clusters = [
            {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
            for i, (c, ids) in enumerate(zip(clusters_raw, all_face_ids))
        ]
        from datetime import datetime, timezone
        cache_data = json.dumps({
            "clusters": cache_clusters,
            "truncated": truncated,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        meta.set_value("face_clusters_cache", cache_data)
        meta.set_value("face_clusters_dirty", "false")
    else:
        try:
            cache_clusters = json.loads(cached).get("clusters", [])
        except (json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=500, detail="Cluster cache corrupted")

    # Find the cluster
    if cluster_index < 0 or cluster_index >= len(cache_clusters):
        raise HTTPException(status_code=404, detail="Cluster not found")

    face_ids = cache_clusters[cluster_index].get("face_ids", [])
    if not face_ids:
        raise HTTPException(status_code=404, detail="Cluster has no faces")

    person_repo = PersonRepository(session)

    if body.person_id:
        # Assign to existing person
        person = person_repo.get_by_id(body.person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        # Assign each face (skip already-assigned)
        from sqlalchemy import text as sa_text
        already = {r[0] for r in session.execute(
            sa_text("SELECT face_id FROM face_person_matches WHERE face_id = ANY(:fids)"),
            {"fids": face_ids},
        ).all()}
        new_face_ids = [fid for fid in face_ids if fid not in already]
        if new_face_ids:
            person_repo._assign_faces(body.person_id, new_face_ids)
            person_repo._recompute_centroid(body.person_id)
            from src.server.repository.tenant import _mark_clusters_dirty
            _mark_clusters_dirty(session)
            session.commit()
    else:
        # Create new person with all cluster faces. has_name guarantees
        # display_name is non-None and non-empty here.
        assert body.display_name is not None
        person = person_repo.create(body.display_name.strip(), face_ids=face_ids)

    face_count = person_repo.get_face_count(person.person_id)

    rep_asset_id = None
    if person.representative_face_id:
        from src.server.models.tenant import Face
        rep_face = session.get(Face, person.representative_face_id)
        if rep_face:
            rep_asset_id = rep_face.asset_id

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=person.representative_face_id,
        representative_asset_id=rep_asset_id,
        confirmation_count=person.confirmation_count,
    )


class DismissResult(BaseModel):
    person_id: str


@faces_router.post("/clusters/{cluster_index}/dismiss", response_model=DismissResult)
def dismiss_cluster(
    cluster_index: int,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> DismissResult:
    """Dismiss a cluster by creating a dismissed person.

    All faces are assigned to the dismissed person. Future similar faces
    will be auto-absorbed by the upkeep propagation job, preventing the
    cluster from reforming. Returns the person_id for undo support.
    """
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import PersonRepository, FaceRepository, _mark_clusters_dirty

    meta = SystemMetadataRepository(session)
    cached = meta.get_value("face_clusters_cache")

    if not cached:
        repo = FaceRepository(session)
        clusters_raw, all_face_ids, truncated = repo.compute_clusters(
            max_clusters=50, faces_per_cluster=20,
        )
        cache_clusters = [
            {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
            for i, (c, ids) in enumerate(zip(clusters_raw, all_face_ids))
        ]
        from datetime import datetime, timezone
        cache_data = json.dumps({
            "clusters": cache_clusters,
            "truncated": truncated,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        meta.set_value("face_clusters_cache", cache_data)
        meta.set_value("face_clusters_dirty", "false")
    else:
        try:
            cache_clusters = json.loads(cached).get("clusters", [])
        except (json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=500, detail="Cluster cache corrupted")

    if cluster_index < 0 or cluster_index >= len(cache_clusters):
        raise HTTPException(status_code=404, detail="Cluster not found")

    face_ids = cache_clusters[cluster_index].get("face_ids", [])
    if not face_ids:
        raise HTTPException(status_code=404, detail="Cluster has no faces")

    person_repo = PersonRepository(session)
    person = person_repo.create_dismissed(face_ids=face_ids)
    _mark_clusters_dirty(session)
    session.commit()
    return DismissResult(person_id=person.person_id)


@faces_router.get("/clusters/{cluster_index}/nearest-people", response_model=list[NearestPersonItem])
def nearest_people_for_cluster(
    cluster_index: int,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = 5,
) -> list[NearestPersonItem]:
    """Return named people sorted by cosine distance to the cluster centroid.

    Computes the cluster centroid from its face embeddings, then ranks all
    non-dismissed people by distance to that centroid.
    """
    import numpy as np
    from sqlalchemy import text as sa_text
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import FaceRepository

    if limit > 20:
        limit = 20

    # --- Get cluster face IDs from cache ---
    meta = SystemMetadataRepository(session)
    cached = meta.get_value("face_clusters_cache")

    if not cached:
        repo = FaceRepository(session)
        clusters_raw, all_face_ids, truncated = repo.compute_clusters(
            max_clusters=50, faces_per_cluster=20,
        )
        cache_clusters = [
            {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
            for i, (c, ids) in enumerate(zip(clusters_raw, all_face_ids))
        ]
        from datetime import datetime, timezone
        cache_data = json.dumps({
            "clusters": cache_clusters,
            "truncated": truncated,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        meta.set_value("face_clusters_cache", cache_data)
        meta.set_value("face_clusters_dirty", "false")
    else:
        try:
            cache_clusters = json.loads(cached).get("clusters", [])
        except (json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=500, detail="Cluster cache corrupted")

    if cluster_index < 0 or cluster_index >= len(cache_clusters):
        raise HTTPException(status_code=404, detail="Cluster not found")

    face_ids = cache_clusters[cluster_index].get("face_ids", [])
    if not face_ids:
        return []

    # --- Compute cluster centroid ---
    # Sample up to 100 faces for centroid computation
    sample_ids = face_ids[:100]
    rows = session.execute(
        sa_text("SELECT embedding_vector::text FROM faces WHERE face_id = ANY(:fids) AND embedding_vector IS NOT NULL"),
        {"fids": sample_ids},
    ).all()
    if not rows:
        return []

    vectors = np.array(
        [[float(x) for x in r[0].strip("[]").split(",")] for r in rows],
        dtype=np.float32,
    )
    centroid = vectors.mean(axis=0)
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm

    # --- Rank people by distance to cluster centroid ---
    people_rows = session.execute(
        sa_text("""
            SELECT p.person_id, p.display_name, p.centroid_vector::text,
                   COUNT(m.match_id)::int AS face_count
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            WHERE p.dismissed = false AND p.centroid_vector IS NOT NULL
            GROUP BY p.person_id
        """),
    ).all()

    if not people_rows:
        return []

    results = []
    for row in people_rows:
        pvec = np.array([float(x) for x in row[2].strip("[]").split(",")], dtype=np.float32)
        pnorm = np.linalg.norm(pvec)
        if pnorm > 0:
            pvec = pvec / pnorm
        dist = float(1.0 - np.dot(centroid, pvec))
        results.append(NearestPersonItem(
            person_id=row[0],
            display_name=row[1],
            face_count=row[3],
            distance=round(dist, 4),
        ))

    results.sort(key=lambda x: x.distance)
    return results[:limit]


@faces_router.get("/{face_id}/nearest-people", response_model=list[NearestPersonItem])
def nearest_people_for_face(
    face_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = 5,
) -> list[NearestPersonItem]:
    """Return named people sorted by cosine distance to a single face's embedding.

    Used by the lightbox face-assignment popover so the dropdown of
    candidate people is ordered by who actually looks most like the
    clicked face — instead of by overall face count, which has no
    bearing on whether they're the right person. The clicked face's
    own embedding is the source vector; people are ranked the same
    way ``nearest_people_for_cluster`` ranks against a cluster
    centroid (1 − cosine similarity, ascending).

    Returns an empty list — not 404 — when the face exists but has no
    embedding, so the popover can still show the alphabetical
    fallback list without an error path.
    """
    import numpy as np
    from sqlalchemy import text as sa_text
    from src.server.models.tenant import Face

    if limit > 20:
        limit = 20

    face = session.get(Face, face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")
    if face.embedding_vector is None:
        return []

    # The pgvector adapter returns the column as a numpy array via the
    # ORM, so feed it directly into np.asarray. (The cluster endpoint
    # uses ::text + .strip("[]").split(",") because it pulls a raw row
    # rather than going through the ORM.)
    src = np.asarray(face.embedding_vector, dtype=np.float32)
    src_norm = float(np.linalg.norm(src))
    if src_norm > 0:
        src = src / src_norm

    people_rows = session.execute(
        sa_text("""
            SELECT p.person_id, p.display_name, p.centroid_vector::text,
                   COUNT(m.match_id)::int AS face_count
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            LEFT JOIN faces f ON f.face_id = m.face_id
            LEFT JOIN assets a
                ON a.asset_id = f.asset_id AND a.deleted_at IS NULL
            WHERE p.dismissed = false AND p.centroid_vector IS NOT NULL
            GROUP BY p.person_id
        """),
    ).all()

    if not people_rows:
        return []

    results = []
    for row in people_rows:
        pvec = np.array(
            [float(x) for x in row[2].strip("[]").split(",")],
            dtype=np.float32,
        )
        pnorm = float(np.linalg.norm(pvec))
        if pnorm > 0:
            pvec = pvec / pnorm
        dist = float(1.0 - np.dot(src, pvec))
        results.append(NearestPersonItem(
            person_id=row[0],
            display_name=row[1],
            face_count=row[3],
            distance=round(dist, 4),
        ))

    results.sort(key=lambda x: x.distance)
    return results[:limit]


class ClusterFacesResponse(BaseModel):
    items: list[PersonFaceItem]
    total: int
    next_cursor: str | None = None


@faces_router.get("/clusters/{cluster_index}/faces", response_model=ClusterFacesResponse)
def list_cluster_faces(
    cluster_index: int,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 50,
) -> ClusterFacesResponse:
    """List all faces in a cluster, cursor-paginated."""
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import FaceRepository
    from src.server.models.tenant import Face, Asset
    from sqlmodel import select

    if limit > 100:
        limit = 100

    # Get cluster face IDs from cache
    meta = SystemMetadataRepository(session)
    cached = meta.get_value("face_clusters_cache")

    if not cached:
        repo = FaceRepository(session)
        clusters_raw, all_face_ids, truncated = repo.compute_clusters(
            max_clusters=50, faces_per_cluster=20,
        )
        cache_clusters = [
            {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
            for i, (c, ids) in enumerate(zip(clusters_raw, all_face_ids))
        ]
        from datetime import datetime, timezone
        cache_data = json.dumps({
            "clusters": cache_clusters,
            "truncated": truncated,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        })
        meta.set_value("face_clusters_cache", cache_data)
        meta.set_value("face_clusters_dirty", "false")
    else:
        try:
            cache_clusters = json.loads(cached).get("clusters", [])
        except (json.JSONDecodeError, KeyError):
            raise HTTPException(status_code=500, detail="Cluster cache corrupted")

    if cluster_index < 0 or cluster_index >= len(cache_clusters):
        raise HTTPException(status_code=404, detail="Cluster not found")

    all_face_ids = cache_clusters[cluster_index].get("face_ids", [])
    total = len(all_face_ids)

    # Apply cursor pagination over the sorted face ID list
    if after:
        try:
            start = all_face_ids.index(after) + 1
        except ValueError:
            start = 0
    else:
        start = 0

    page_ids = all_face_ids[start:start + limit]
    if not page_ids:
        return ClusterFacesResponse(items=[], total=total, next_cursor=None)

    # Load face + asset data
    stmt = select(Face).where(Face.face_id.in_(page_ids))
    faces_by_id = {f.face_id: f for f in session.exec(stmt).all()}

    asset_ids = list({faces_by_id[fid].asset_id for fid in page_ids if fid in faces_by_id})
    asset_map: dict[str, tuple[str, object]] = {}
    if asset_ids:
        rows = session.exec(
            select(Asset.asset_id, Asset.rel_path, Asset.taken_at).where(Asset.asset_id.in_(asset_ids))
        ).all()
        asset_map = {r[0]: (r[1], r[2]) for r in rows}

    items = []
    for fid in page_ids:
        f = faces_by_id.get(fid)
        if not f:
            continue
        rel, taken = asset_map.get(f.asset_id) or (None, None)
        items.append(PersonFaceItem(
            face_id=f.face_id,
            asset_id=f.asset_id,
            bounding_box=f.bounding_box_json,
            detection_confidence=f.detection_confidence,
            rel_path=rel,
            taken_at=taken.isoformat() if taken is not None else None,
        ))

    next_cursor = page_ids[-1] if len(page_ids) == limit and start + limit < total else None

    return ClusterFacesResponse(items=items, total=total, next_cursor=next_cursor)


@faces_router.get("/{face_id}/crop")
def get_face_crop(
    face_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
):
    """Serve the 128x128 face crop thumbnail. Generates on demand if missing."""
    from fastapi.responses import StreamingResponse

    from src.server.models.tenant import Face
    from src.server.storage.local import get_storage

    face = session.get(Face, face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")

    storage = get_storage()

    # Generate crop on demand if missing
    if not face.crop_key or not storage.abs_path(face.crop_key).exists():
        if not face.bounding_box_json:
            raise HTTPException(status_code=404, detail="No bounding box for this face")
        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            raise HTTPException(status_code=404, detail="No crop available")
        from src.server.repository.tenant import AssetRepository
        asset = AssetRepository(session).get_by_id(face.asset_id)
        if not asset or not asset.proxy_key:
            raise HTTPException(status_code=404, detail="No proxy available to generate crop")
        from src.server.api.routers.assets import _generate_face_crops
        _generate_face_crops(tenant_id, asset, [face_id], [{"bounding_box": face.bounding_box_json}], session)
        session.refresh(face)
        if not face.crop_key:
            raise HTTPException(status_code=404, detail="Crop generation failed")

    crop_path = storage.abs_path(face.crop_key)
    if not crop_path.exists():
        raise HTTPException(status_code=404, detail="Crop file missing")

    return StreamingResponse(
        open(crop_path, "rb"),
        media_type="image/webp",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@faces_router.post("/{face_id}/assign", status_code=200)
def assign_face(
    face_id: str,
    body: FaceAssignRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Assign a face to a person (existing or new).

    Provide either person_id (existing) or new_person_name (creates new person).
    Returns 409 if face is already assigned.
    """
    from src.server.repository.tenant import PersonRepository
    from src.server.models.tenant import Face

    if not body.person_id and not body.new_person_name:
        raise HTTPException(status_code=400, detail="Provide person_id or new_person_name")
    if body.person_id and body.new_person_name:
        raise HTTPException(status_code=400, detail="Provide person_id or new_person_name, not both")

    # Verify face exists
    face = session.get(Face, face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")

    repo = PersonRepository(session)

    # Check if already assigned
    from sqlalchemy import text as sa_text
    existing = session.execute(
        sa_text("SELECT person_id FROM face_person_matches WHERE face_id = :fid"),
        {"fid": face_id},
    ).scalar()
    if existing:
        raise HTTPException(status_code=409, detail={
            "message": "Face already assigned",
            "current_person_id": existing,
        })

    if body.new_person_name:
        person = repo.create(body.new_person_name.strip(), face_ids=[face_id])
        return {"person_id": person.person_id, "display_name": person.display_name}
    else:
        person = repo.get_by_id(body.person_id)  # type: ignore[arg-type]
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
        repo.assign_face(face_id, body.person_id, confirmed=True)  # type: ignore[arg-type]
        return {"person_id": person.person_id, "display_name": person.display_name}


@faces_router.delete("/{face_id}/assign", status_code=204)
def unassign_face(
    face_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Remove a face from its assigned person."""
    from src.server.repository.tenant import PersonRepository

    repo = PersonRepository(session)
    if not repo.unassign_face(face_id):
        raise HTTPException(status_code=404, detail="Face assignment not found")


@faces_router.get("/clusters", response_model=ClustersResponse)
def get_clusters(
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = 20,
    faces_per_cluster: int = 6,
    min_cluster_size: int = 2,
) -> ClustersResponse:
    """Return clusters of unassigned faces. Uses cache; recomputes if dirty."""
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import FaceRepository

    if limit > 50:
        limit = 50
    if faces_per_cluster > 20:
        faces_per_cluster = 20
    if min_cluster_size < 1:
        min_cluster_size = 1

    meta = SystemMetadataRepository(session)
    dirty = meta.get_value("face_clusters_dirty")
    cached = meta.get_value("face_clusters_cache")

    def _build_response(all_clusters: list[dict], truncated: bool) -> ClustersResponse:
        """Filter by min_cluster_size, apply limit, return response with max_cluster_size."""
        max_size = max((c["size"] for c in all_clusters), default=0)
        filtered = [c for c in all_clusters if c["size"] >= min_cluster_size][:limit]
        return ClustersResponse(
            clusters=[
                ClusterItem(cluster_index=c["cluster_index"], size=c["size"], faces=c.get("faces", [])[:faces_per_cluster])
                for c in filtered
            ],
            truncated=truncated,
            max_cluster_size=max_size,
        )

    # Return cache if clean and exists
    if not dirty and cached:
        try:
            data = json.loads(cached)
            return _build_response(data.get("clusters", []), data.get("truncated", False))
        except (json.JSONDecodeError, KeyError):
            pass  # corrupted cache, recompute

    # Compute fresh clusters
    repo = FaceRepository(session)
    clusters_raw, all_face_ids, truncated = repo.compute_clusters(
        max_clusters=50,  # cache max, apply limit on read
        faces_per_cluster=20,  # cache max
    )

    # Build cache payload — includes all face IDs per cluster for server-side naming
    cache_clusters = [
        {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
        for i, (c, ids) in enumerate(zip(clusters_raw, all_face_ids))
    ]
    from datetime import datetime, timezone
    cache_data = json.dumps({
        "clusters": cache_clusters,
        "truncated": truncated,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })
    meta.set_value("face_clusters_cache", cache_data)
    meta.set_value("face_clusters_dirty", "false")

    return _build_response(cache_clusters, truncated)
