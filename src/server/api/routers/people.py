"""People endpoints: CRUD for named people, face clustering."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_editor
from src.server.api.errors import ConflictError
from src.server.api.library_access import edited, names, seen
from src.server.api.limits import MAX_IDS
from src.server.repository.faces import current_face_model

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
    face_ids: list[str] | None = Field(default=None, max_length=MAX_IDS)


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
    media_type: str | None = None
    # A video's face (one a person seen): the time of the frame its box and
    # crop are from, and every time they were seen at. None for a photo's.
    frame_ms: int | None = None
    seen_ms: list[int] | None = None


class PersonFacesResponse(BaseModel):
    items: list[PersonFaceItem]
    next_cursor: str | None = None


class ClusterItem(BaseModel):
    # Names the cluster by its faces (cluster_id_of): the same faces, the same
    # id, whenever it was computed. A route given one that no longer matches a
    # cluster answers 409 cluster_changed.
    cluster_id: str
    size: int
    faces: list[dict]
    # Its latest photo: taken, else the file's time (None: no photo says).
    newest: datetime | None = None


class ClustersResponse(BaseModel):
    clusters: list[ClusterItem]
    truncated: bool = False
    max_cluster_size: int = 0
    # When these were computed (None: never yet), and whether faces changed
    # since, so upkeep will compute them again (every 5 minutes).
    computed_at: datetime | None = None
    pending: bool = False


# ---------- What the caller sees (ADR-018 decision 8, roles per library) ----------


def _people(request: Request, session: Session):
    """The people repository as the caller sees it: people with a face in a
    library they can see, and only those faces."""
    from src.server.repository.people import PersonRepository

    return PersonRepository(session, within=seen(request, session))


def _person_or_404(repo, person_id: str | None, detail: str = "Person not found"):
    """The person, when the caller sees them; 404 otherwise, as for one that isn't there."""
    person = repo.get_by_id(person_id) if person_id else None
    if person is None or not repo.shows(person):
        raise HTTPException(status_code=404, detail=detail)
    return person


# ---------- Endpoints ----------

@router.get("", response_model=PersonListResponse)
def list_people(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    q: str | None = None,
) -> PersonListResponse:
    """List people sorted by face count descending (those the caller sees,
    by the faces they see). Optional q param for name search."""
    repo = _people(request, session)
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
        if face_count > 0:
            repo.ensure_representative(person)
        items.append(_person_item(repo, person, face_count))

    next_cursor: str | None = None
    if items and len(items) == limit:
        last = rows[-1]
        cursor_data = {"count": last[1], "id": last[0].person_id}
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(cursor_data).encode()
        ).decode().rstrip("=")

    return PersonListResponse(items=items, next_cursor=next_cursor)


@router.post("", response_model=PersonItem, status_code=201, dependencies=[Depends(require_editor)])
@names("body.face_ids")
def create_person(
    body: PersonCreateRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Create a new named person. Optionally assign face_ids."""
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")

    repo = _people(request, session)

    # Check if any face_ids are already assigned
    if body.face_ids:
        from src.server.repository.faces import FaceRepository

        if already := FaceRepository(session).assigned_among(body.face_ids):
            conflicts = [{"face_id": face_id, "person_id": person_id} for face_id, person_id in already.items()]
            raise ConflictError("faces_assigned", "Faces already assigned", {"conflicts": conflicts})

    person = repo.create(body.display_name.strip(), face_ids=body.face_ids)
    return _person_item(repo, person, repo.get_face_count(person.person_id))


@router.get("/dismissed", response_model=PersonListResponse)
def list_dismissed_people(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> PersonListResponse:
    """List dismissed people sorted by face count descending (those the caller sees)."""
    repo = _people(request, session)
    rows = repo.list_dismissed(after=after, limit=limit)

    items = []
    for person, face_count in rows:
        # Backfill representative face if missing (for dismissed people created before the fix)
        if face_count > 0:
            repo.ensure_representative(person)
        items.append(_person_item(repo, person, face_count))

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


def _person_item(repo, person, face_count: int) -> PersonItem:
    """A person as the API shows them, with their representative face's clip (their thumbnail)."""
    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=repo.representative_face_id(person),
        representative_asset_id=repo.representative_asset_id(person),
        confirmation_count=person.confirmation_count,
    )


def _nearest(repo, vector, limit: int, *, exclude_id: str | None = None) -> list[NearestPersonItem]:
    """Named people by cosine distance (1 − similarity) from `vector` to their
    centroids in the account's face model, nearest first: those the caller
    sees (repo's)."""
    import numpy as np

    src = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(src))
    if norm > 0:
        src = src / norm
    results = []
    for person_id, display_name, centroid, face_count in repo.named_centroids(
            current_face_model(repo._session), exclude_id=exclude_id):
        pvec = np.array(centroid, dtype=np.float32)
        pnorm = float(np.linalg.norm(pvec))
        if pnorm > 0:
            pvec = pvec / pnorm
        results.append(NearestPersonItem(person_id=person_id, display_name=display_name, face_count=face_count,
                                         distance=round(float(1.0 - np.dot(src, pvec)), 4)))
    results.sort(key=lambda x: x.distance)
    return results[:limit]


@router.get("/{person_id}/nearest", response_model=list[NearestPersonItem])
def nearest_people_for_person(
    person_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=5, ge=1, le=20),
) -> list[NearestPersonItem]:
    """Return named people sorted by cosine distance to this person's centroid."""
    repo = _people(request, session)
    person = repo.get_by_id(person_id)
    if person is None or not repo.shows(person) or person.centroid_vector is None \
            or person.centroid_model != current_face_model(session):
        return []
    return _nearest(repo, person.centroid_vector, limit, exclude_id=person_id)


class UndismissRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)


@router.post("/{person_id}/undismiss", response_model=PersonItem, dependencies=[Depends(require_editor)])
def undismiss_person(
    person_id: str,
    body: UndismissRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Restore a dismissed person and give them a name."""
    repo = _people(request, session)
    person = _person_or_404(repo, person_id, "Dismissed person not found")
    if not person.dismissed:
        raise HTTPException(status_code=404, detail="Dismissed person not found")

    repo.undismiss(person, body.display_name.strip())
    return _person_item(repo, person, repo.get_face_count(person_id))


@router.get("/{person_id}", response_model=PersonItem)
def get_person(
    person_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Get a person by ID."""
    repo = _people(request, session)
    person = _person_or_404(repo, person_id)

    return _person_item(repo, person, repo.get_face_count(person_id))


@router.patch("/{person_id}", response_model=PersonItem, dependencies=[Depends(require_editor)])
def update_person(
    person_id: str,
    body: PersonUpdateRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Update a person's display name."""
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name must not be empty")

    repo = _people(request, session)
    _person_or_404(repo, person_id)
    person = repo.update_name(person_id, body.display_name.strip())
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")

    face_count = repo.get_face_count(person_id)

    return PersonItem(
        person_id=person.person_id,
        display_name=person.display_name,
        face_count=face_count,
        representative_face_id=repo.representative_face_id(person),
        confirmation_count=person.confirmation_count,
    )


@router.delete("/{person_id}", status_code=204, dependencies=[Depends(require_editor)])
def delete_person(
    person_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Delete a person and all their face matches."""
    repo = _people(request, session)
    _person_or_404(repo, person_id)
    if not repo.delete(person_id):
        raise HTTPException(status_code=404, detail="Person not found")


@router.get("/{person_id}/faces", response_model=PersonFacesResponse)
def list_person_faces(
    person_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> PersonFacesResponse:
    """List faces matched to a person (those the caller sees), cursor-paginated."""
    repo = _people(request, session)
    _person_or_404(repo, person_id)

    items = _face_items(session, repo.get_faces(person_id, after=after, limit=limit))

    next_cursor: str | None = None
    if items and len(items) == limit:
        next_cursor = items[-1].face_id

    return PersonFacesResponse(items=items, next_cursor=next_cursor)


def _face_items(session: Session, faces: list) -> list[PersonFaceItem]:
    """Faces as a person's or a cluster's list shows them, with their clips' path, date and media."""
    from src.server.repository.assets import AssetRepository

    clips = AssetRepository(session).path_date_media(list({f.asset_id for f in faces}))
    items = []
    for f in faces:
        rel, taken, media = clips.get(f.asset_id) or (None, None, None)
        items.append(PersonFaceItem(
            face_id=f.face_id,
            asset_id=f.asset_id,
            bounding_box=f.bounding_box_json,
            detection_confidence=f.detection_confidence,
            rel_path=rel,
            taken_at=taken.isoformat() if taken is not None else None,
            media_type=media,
            frame_ms=f.frame_ms,
            seen_ms=list(f.seen_ms) if f.seen_ms is not None else None,
        ))
    return items


@router.post("/{person_id}/merge", response_model=PersonItem, dependencies=[Depends(require_editor)])
def merge_person(
    person_id: str,
    body: MergeRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Merge source person into target (person_id). Source is deleted."""
    if body.source_person_id == person_id:
        raise HTTPException(status_code=400, detail="Cannot merge a person into themselves")

    repo = _people(request, session)
    _person_or_404(repo, person_id)
    _person_or_404(repo, body.source_person_id)
    target = repo.merge(person_id, body.source_person_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Person not found")

    return _person_item(repo, target, repo.get_face_count(person_id))


# ---------- Clusters endpoint (on /v1/faces prefix) ----------

faces_router = APIRouter(prefix="/v1/faces", tags=["faces"])


class ClusterNameRequest(BaseModel):
    # Either display_name (create new person) or person_id (merge into
    # existing). The handler validates exactly one is provided. Both
    # are optional at the schema level so Pydantic doesn't reject
    # merge-only requests before the handler can run.
    display_name: str | None = None
    person_id: str | None = None  # assign to existing person instead of creating new


@faces_router.post("/clusters/{cluster_id}/name", response_model=PersonItem, status_code=201, dependencies=[Depends(require_editor)])
def name_cluster(
    cluster_id: str,
    body: ClusterNameRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PersonItem:
    """Name all faces in a cluster (those in libraries the caller edits).
    Creates a new person or assigns to existing.

    409 cluster_changed when no cluster has this id any more, or some of its
    faces were named or dismissed since: load the clusters again.
    """
    has_name = body.display_name is not None and body.display_name.strip() != ""
    if not has_name and not body.person_id:
        raise HTTPException(status_code=400, detail="Provide display_name or person_id")

    face_ids = _unassigned_cluster_faces(session, cluster_id, request)
    person_repo = _people(request, session)

    if body.person_id:
        # Assign to existing person
        person = _person_or_404(person_repo, body.person_id)
        # Assign each face (skip already-assigned)
        from src.server.repository.faces import FaceRepository

        already = FaceRepository(session).assigned_among(face_ids)
        if new_face_ids := [fid for fid in face_ids if fid not in already]:
            person_repo.assign_to(body.person_id, new_face_ids)
    else:
        # Create new person with all cluster faces. has_name guarantees
        # display_name is non-None and non-empty here.
        assert body.display_name is not None
        person = person_repo.create(body.display_name.strip(), face_ids=face_ids)

    return _person_item(person_repo, person, person_repo.get_face_count(person.person_id))


class DismissResult(BaseModel):
    person_id: str


@faces_router.post("/clusters/{cluster_id}/dismiss", response_model=DismissResult, dependencies=[Depends(require_editor)])
def dismiss_cluster(
    cluster_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> DismissResult:
    """Dismiss a cluster by creating a dismissed person.

    All faces are assigned to the dismissed person. Future similar faces
    will be auto-absorbed by the upkeep propagation job, preventing the
    cluster from reforming. Returns the person_id for undo support.
    409 cluster_changed as for naming.
    """
    from src.server.repository.faces import _mark_clusters_dirty
    from src.server.repository.people import PersonRepository

    face_ids = _unassigned_cluster_faces(session, cluster_id, request)
    person_repo = PersonRepository(session)
    person = person_repo.create_dismissed(face_ids=face_ids)
    _mark_clusters_dirty(session)
    session.commit()
    return DismissResult(person_id=person.person_id)


@faces_router.get("/clusters/{cluster_id}/nearest-people", response_model=list[NearestPersonItem])
def nearest_people_for_cluster(
    cluster_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=5, ge=1, le=20),
) -> list[NearestPersonItem]:
    """Return named people sorted by cosine distance to the cluster centroid.

    Computes the cluster centroid from its face embeddings, then ranks all
    non-dismissed people by distance to that centroid.
    """
    import numpy as np

    from src.server.repository.faces import FaceRepository

    face_ids = find_cluster(session, cluster_id, request).get("face_ids", [])
    if not face_ids:
        return []

    # --- Compute cluster centroid ---
    # Sample up to 100 faces for centroid computation
    vectors = FaceRepository(session).embeddings(face_ids[:100], current_face_model(session))
    if not vectors:
        return []
    return _nearest(_people(request, session), np.array(vectors, dtype=np.float32).mean(axis=0), limit)


@faces_router.get("/{face_id}/nearest-people", response_model=list[NearestPersonItem])
def nearest_people_for_face(
    face_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=5, ge=1, le=20),
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
    embedding, or one from another face model than the account's (mid
    switch: another space than the people's centroids), so the popover
    can still show the alphabetical fallback list without an error path.
    """
    from src.server.repository.faces import FaceRepository

    face = FaceRepository(session).get(face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")
    model = current_face_model(session)
    if face.embedding_vector is None or face.embedding_model != model:
        return []

    # The pgvector adapter returns the column as a numpy array via the ORM.
    return _nearest(_people(request, session), face.embedding_vector, limit)


class ClusterFacesResponse(BaseModel):
    items: list[PersonFaceItem]
    total: int
    next_cursor: str | None = None


@faces_router.get("/clusters/{cluster_id}/faces", response_model=ClusterFacesResponse)
def list_cluster_faces(
    cluster_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ClusterFacesResponse:
    """List all faces in a cluster (those the caller sees), cursor-paginated.
    409 cluster_changed when no cluster has this id any more."""
    from src.server.repository.faces import FaceRepository

    all_face_ids = find_cluster(session, cluster_id, request).get("face_ids", [])
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
    faces_by_id = FaceRepository(session).get_many(page_ids)
    items = _face_items(session, [faces_by_id[fid] for fid in page_ids if fid in faces_by_id])

    next_cursor = page_ids[-1] if len(page_ids) == limit and start + limit < total else None

    return ClusterFacesResponse(items=items, total=total, next_cursor=next_cursor)


@faces_router.get("/{face_id}/crop")
def get_face_crop(
    face_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
):
    """Serve the 128x128 face crop thumbnail. Generates on demand if missing."""
    from fastapi.responses import Response

    from src.server.repository.faces import FaceRepository
    from src.server.storage.store import NotFound, get_store

    faces = FaceRepository(session)
    face = faces.get(face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="Face not found")
    crop_key = face.crop_key

    store = get_store()

    # Generate crop on demand if missing
    if not crop_key or not store.exists(crop_key):
        if face.frame_ms is not None:  # a video's: cut from its frame when found, never from the poster
            raise HTTPException(status_code=404, detail="No crop for this face")
        if not face.bounding_box_json:
            raise HTTPException(status_code=404, detail="No bounding box for this face")
        tenant_id = getattr(request.state, "tenant_id", None)
        if not tenant_id:
            raise HTTPException(status_code=404, detail="No crop available")
        from src.server.repository.assets import AssetRepository
        asset = AssetRepository(session).get_by_id(face.asset_id)
        if not asset or not asset.proxy_key:
            raise HTTPException(status_code=404, detail="No proxy available to generate crop")
        from src.server.api.routers.assets import _generate_face_crops
        _generate_face_crops(tenant_id, asset, [face_id], [{"bounding_box": face.bounding_box_json}], session)
        crop_key = faces.crop_key_of(face_id)  # as it was saved
        if not crop_key:
            raise HTTPException(status_code=404, detail="Crop generation failed")

    try:
        crop = store.read(crop_key)  # a 128 x 128 WebP: read whole
    except NotFound:
        raise HTTPException(status_code=404, detail="Crop file missing") from None

    # A person's face: kept by the browser that asked, never by a cache between (private).
    return Response(crop, media_type="image/webp", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@faces_router.post("/{face_id}/assign", status_code=200, dependencies=[Depends(require_editor)])
def assign_face(
    face_id: str,
    body: FaceAssignRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> dict:
    """Assign a face to a person (existing or new).

    Provide either person_id (existing) or new_person_name (creates new person).
    Returns 409 if face is already assigned.
    """
    from src.server.repository.faces import FaceRepository

    if not body.person_id and not body.new_person_name:
        raise HTTPException(status_code=400, detail="Provide person_id or new_person_name")
    if body.person_id and body.new_person_name:
        raise HTTPException(status_code=400, detail="Provide person_id or new_person_name, not both")

    # Verify face exists
    faces = FaceRepository(session)
    if faces.get(face_id) is None:
        raise HTTPException(status_code=404, detail="Face not found")

    repo = _people(request, session)

    # Check if already assigned
    if existing := faces.assigned_among([face_id]).get(face_id):
        raise ConflictError("face_assigned", "Face already assigned", {"current_person_id": existing})

    if body.new_person_name:
        person = repo.create(body.new_person_name.strip(), face_ids=[face_id])
        return {"person_id": person.person_id, "display_name": person.display_name}
    else:
        person = _person_or_404(repo, body.person_id)
        repo.assign_face(face_id, body.person_id, confirmed=True)  # type: ignore[arg-type]
        return {"person_id": person.person_id, "display_name": person.display_name}


@faces_router.delete("/{face_id}/assign", status_code=204, dependencies=[Depends(require_editor)])
def unassign_face(
    face_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> None:
    """Remove a face from its assigned person."""
    from src.server.repository.people import PersonRepository

    repo = PersonRepository(session)
    if not repo.unassign_face(face_id):
        raise HTTPException(status_code=404, detail="Face assignment not found")


# The orders unnamed clusters come in (GET /v1/faces/clusters?sort=), the first the default.
CLUSTER_SORTS = ("size_desc", "size_asc", "newest")
ClusterSort = Literal["size_desc", "size_asc", "newest"]


def order_clusters(clusters: list[dict], sort: str) -> list[dict]:
    """Cached clusters in the order asked: size_desc, largest first (naming
    them makes the most progress); size_asc, smallest first; newest, the one
    with the latest photo first (undated last). Ties go by the cluster's
    first face id, which stays while its faces do, so the order doesn't
    shuffle between loads."""
    def first_face(c: dict) -> str:
        return min(c.get("face_ids") or [""])

    by_face = sorted(clusters, key=first_face)
    if sort == "size_asc":
        return sorted(by_face, key=lambda c: c["size"])
    if sort == "newest":
        dated = sorted((c for c in by_face if c.get("newest")), key=lambda c: c["newest"], reverse=True)
        return dated + [c for c in by_face if not c.get("newest")]
    return sorted(by_face, key=lambda c: -c["size"])


def cluster_id_of(face_ids: list[str]) -> str:
    """A cluster's id: a fingerprint of its face ids, in any order."""
    import hashlib

    return "fc_" + hashlib.sha256("\n".join(sorted(face_ids)).encode()).hexdigest()[:20]


def store_clusters(session: Session) -> dict:
    """Compute every cluster and keep them as the cluster cache, with the
    version they were computed at. The version is read first: a change
    committed while computing makes the cache old at once, never current.

    Run by upkeep (POST /v1/upkeep when the cache is old, and
    /v1/upkeep/recluster), never by a request that reads them."""
    from src.server.repository.faces import FaceRepository, face_clusters_version
    from src.server.repository.system_metadata import SystemMetadataRepository

    version = face_clusters_version(session)
    data = {**FaceRepository(session).cluster_cache(), "version": version}
    SystemMetadataRepository(session).set_value("face_clusters_cache", json.dumps(data))
    return data


def cached_clusters(session: Session) -> dict | None:
    """The cluster cache as last computed, current or not; None when there's none."""
    from src.server.repository.system_metadata import SystemMetadataRepository

    cached = SystemMetadataRepository(session).get_value("face_clusters_cache")
    if not cached:
        return None
    try:
        data = json.loads(cached)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def clusters_current(session: Session, data: dict | None) -> bool:
    """Nothing changed which faces are clustered since the cache was computed."""
    from src.server.repository.faces import face_clusters_version

    return data is not None and data.get("version") == face_clusters_version(session)


def find_cluster(session: Session, cluster_id: str, request: Request | None = None) -> dict:
    """The cached cluster with this id, of the faces the caller sees (when a
    request is given); 409 cluster_changed when there's none, as when none
    of its faces is in a library they can see."""
    for c in (cached_clusters(session) or {}).get("clusters", []):
        if cluster_id_of(c.get("face_ids") or []) == cluster_id:
            if request is None:
                return c
            for shown in in_sight_clusters(session, [c], seen(request, session)):
                return shown
            break
    raise ConflictError("cluster_changed", "This group changed. Reload the groups.", {"cluster_id": cluster_id})


def in_sight_clusters(session: Session, clusters: list[dict], within: list[str] | None) -> list[dict]:
    """The clusters as the caller sees them (within: the libraries they can
    see; None, every one): of their faces in those, its size, sample and
    newest photo; one with none of them isn't there. The id stays the
    cluster's own (cluster_id_of its every face)."""
    if within is None:
        return clusters
    from src.server.repository.faces import FaceRepository

    ids = [fid for c in clusters for fid in (c.get("face_ids") or [])]
    where = FaceRepository(session).libraries_and_times(ids)
    libraries = set(within)
    out = []
    for c in clusters:
        mine = [fid for fid in c.get("face_ids") or [] if where.get(fid, (None, None))[0] in libraries]
        if not mine:
            continue
        kept = set(mine)
        dates = [where[fid][1] for fid in mine if where[fid][1] is not None]
        out.append({**c, "id": cluster_id_of(c.get("face_ids") or []), "face_ids": mine, "size": len(mine),
                    "faces": [f for f in c.get("faces", []) if f.get("face_id") in kept],
                    "newest": max(dates).isoformat() if dates else None})
    return out


def _unassigned_cluster_faces(session: Session, cluster_id: str, request: Request | None = None) -> list[str]:
    """The cluster's face ids, none named or dismissed since it was computed;
    409 cluster_changed otherwise. The faces are locked first, so of two
    requests for one cluster the second waits and then gets the 409. With a
    request, the faces the caller edits (403 when they see some, edit none)."""
    from src.server.repository.faces import FaceRepository

    face_ids = find_cluster(session, cluster_id, request).get("face_ids") or []
    if request is not None and face_ids and (libraries := edited(request, session)) is not None:
        where = FaceRepository(session).libraries_and_times(face_ids)
        face_ids = [fid for fid in face_ids if where.get(fid, (None, None))[0] in set(libraries)]
        if not face_ids:
            raise HTTPException(status_code=403, detail="Editor access required")
    taken = FaceRepository(session).lock_counting_assigned(face_ids)
    if not face_ids or taken:
        raise ConflictError("cluster_changed", "This group changed. Reload the groups.", {"cluster_id": cluster_id})
    return face_ids


def _without_assigned(session: Session, clusters: list[dict]) -> list[dict]:
    """The clusters none of whose faces were named or dismissed since they
    were computed: a cluster just named doesn't show before upkeep computes
    them again, and one whose dismissal was undone shows again."""
    from src.server.repository.faces import FaceRepository

    ids = [fid for c in clusters for fid in (c.get("face_ids") or [])]
    if not ids:
        return clusters
    taken = set(FaceRepository(session).assigned_among(ids))
    if not taken:
        return clusters
    return [c for c in clusters if not taken.intersection(c.get("face_ids") or [])]


@faces_router.get("/clusters", response_model=ClustersResponse)
def get_clusters(
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=20, ge=1, le=50),
    faces_per_cluster: int = Query(default=6, ge=1, le=20),
    min_cluster_size: int = Query(default=2, ge=1),
    sort: ClusterSort = "size_desc",
) -> ClustersResponse:
    """Clusters of unassigned faces in the order asked (largest first unless
    asked otherwise; see order_clusters), as upkeep last computed them.
    Reading them computes nothing: computed_at says when, and pending that
    faces changed since (upkeep computes them again within 5 minutes)."""
    data = cached_clusters(session)
    all_clusters = in_sight_clusters(session, _without_assigned(session, (data or {}).get("clusters", [])),
                                     seen(request, session))
    max_size = max((c["size"] for c in all_clusters), default=0)
    shown = order_clusters([c for c in all_clusters if c["size"] >= min_cluster_size], sort)[:limit]
    return ClustersResponse(
        clusters=[
            ClusterItem(cluster_id=c.get("id") or cluster_id_of(c.get("face_ids") or []), size=c["size"],
                        faces=c.get("faces", [])[:faces_per_cluster], newest=c.get("newest"))
            for c in shown
        ],
        truncated=(data or {}).get("truncated", False),
        max_cluster_size=max_size,
        computed_at=(data or {}).get("computed_at"),
        pending=not clusters_current(session, data),
    )
