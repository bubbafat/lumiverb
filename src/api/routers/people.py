"""People endpoints: CRUD for named people, face clustering."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session

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
    display_name: str
    face_ids: list[str] | None = None


class PersonUpdateRequest(BaseModel):
    display_name: str


class FaceAssignRequest(BaseModel):
    person_id: str | None = None
    new_person_name: str | None = None


class MergeRequest(BaseModel):
    source_person_id: str


class PersonFaceItem(BaseModel):
    face_id: str
    asset_id: str
    bounding_box: dict | None = None
    detection_confidence: float | None = None
    rel_path: str | None = None


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


# ---------- Endpoints ----------

@router.get("", response_model=PersonListResponse)
def list_people(
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = 50,
) -> PersonListResponse:
    """List people sorted by face count descending."""
    from src.repository.tenant import PersonRepository, FaceRepository

    if limit > 100:
        limit = 100
    if limit < 1:
        limit = 1

    repo = PersonRepository(session)
    rows = repo.list_with_face_counts(after=after, limit=limit)

    items = []
    for person, face_count in rows:
        # Get representative face's asset_id for thumbnail
        rep_asset_id = None
        if person.representative_face_id:
            from src.models.tenant import Face
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
    from src.repository.tenant import PersonRepository

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
        from src.models.tenant import Face
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
    from src.repository.tenant import PersonRepository

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    rep_asset_id = None
    if person.representative_face_id:
        from src.models.tenant import Face
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
    from src.repository.tenant import PersonRepository

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
    from src.repository.tenant import PersonRepository

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
    from src.repository.tenant import PersonRepository

    if limit > 100:
        limit = 100

    repo = PersonRepository(session)
    person = repo.get_by_id(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    faces = repo.get_faces(person_id, after=after, limit=limit)

    # Get asset rel_paths for each face
    asset_ids = list({f.asset_id for f in faces})
    from src.models.tenant import Asset
    from sqlmodel import select
    asset_map: dict[str, str] = {}
    if asset_ids:
        stmt = select(Asset.asset_id, Asset.rel_path).where(Asset.asset_id.in_(asset_ids))
        asset_map = {r[0]: r[1] for r in session.exec(stmt).all()}

    items = [
        PersonFaceItem(
            face_id=f.face_id,
            asset_id=f.asset_id,
            bounding_box=f.bounding_box_json,
            detection_confidence=f.detection_confidence,
            rel_path=asset_map.get(f.asset_id),
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
    from src.repository.tenant import PersonRepository

    if body.source_person_id == person_id:
        raise HTTPException(status_code=400, detail="Cannot merge a person into themselves")

    repo = PersonRepository(session)
    target = repo.merge(person_id, body.source_person_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    rep_asset_id = None
    if target.representative_face_id:
        from src.models.tenant import Face
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


@faces_router.get("/{face_id}/crop")
def get_face_crop(
    face_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
):
    """Serve the 128x128 face crop thumbnail."""
    from pathlib import Path

    from fastapi.responses import StreamingResponse

    from src.models.tenant import Face
    from src.storage.local import get_storage

    face = session.get(Face, face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")
    if not face.crop_key:
        raise HTTPException(status_code=404, detail="No crop available for this face")

    storage = get_storage()
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
    from src.repository.tenant import PersonRepository
    from src.models.tenant import Face

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
    from src.repository.tenant import PersonRepository

    repo = PersonRepository(session)
    if not repo.unassign_face(face_id):
        raise HTTPException(status_code=404, detail="Face assignment not found")


@faces_router.get("/clusters", response_model=ClustersResponse)
def get_clusters(
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = 20,
    faces_per_cluster: int = 6,
) -> ClustersResponse:
    """Return clusters of unassigned faces. Uses cache; recomputes if dirty."""
    from src.repository.system_metadata import SystemMetadataRepository
    from src.repository.tenant import FaceRepository

    if limit > 50:
        limit = 50
    if faces_per_cluster > 20:
        faces_per_cluster = 20

    meta = SystemMetadataRepository(session)
    dirty = meta.get_value("face_clusters_dirty")
    cached = meta.get_value("face_clusters_cache")

    # Return cache if clean and exists
    if not dirty and cached:
        try:
            data = json.loads(cached)
            # Apply limit/faces_per_cluster to cached data
            clusters = data.get("clusters", [])[:limit]
            for c in clusters:
                c["faces"] = c.get("faces", [])[:faces_per_cluster]
            return ClustersResponse(
                clusters=[
                    ClusterItem(cluster_index=i, size=c["size"], faces=c["faces"])
                    for i, c in enumerate(clusters)
                ],
                truncated=data.get("truncated", False),
            )
        except (json.JSONDecodeError, KeyError):
            pass  # corrupted cache, recompute

    # Compute fresh clusters
    repo = FaceRepository(session)
    clusters_raw, truncated = repo.compute_clusters(
        max_clusters=50,  # cache max, apply limit on read
        faces_per_cluster=20,  # cache max
    )

    # Build cache payload
    cache_clusters = [
        {"cluster_index": i, "size": len(c), "faces": c}
        for i, c in enumerate(clusters_raw)
    ]
    from datetime import datetime, timezone
    cache_data = json.dumps({
        "clusters": cache_clusters,
        "truncated": truncated,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })
    meta.set_value("face_clusters_cache", cache_data)
    meta.set_value("face_clusters_dirty", "false")

    # Apply requested limits
    result_clusters = cache_clusters[:limit]
    for c in result_clusters:
        c["faces"] = c["faces"][:faces_per_cluster]

    return ClustersResponse(
        clusters=[
            ClusterItem(cluster_index=i, size=c["size"], faces=c["faces"])
            for i, c in enumerate(result_clusters)
        ],
        truncated=truncated,
    )
