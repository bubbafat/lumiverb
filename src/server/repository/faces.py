"""Faces found in clips, grouped into clusters by their embeddings. Every repository takes a Session."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session, select

from src.server.after_commit import AfterCommit
from src.server.models.tenant import (
    Asset,
    Face,
    FacePersonMatch,
    Person,
)
from ulid import ULID


def _lock_faces(session: Session, face_ids: list[str]) -> None:
    """Lock these faces until the transaction ends, in face_id order.

    The lock order for whatever finds or names faces: a clip (assets)
    first, then faces in face_id order, before writing any face, name,
    rejection or person (the first write to a face or a name also takes
    the face clusters version row, migration d7f2a3b4c5e6). Re-detection
    (submit_faces), the people routes and upkeep keep to it, so none
    deadlocks another, and a person's word written meanwhile is waited
    for and seen, never lost."""
    if face_ids:
        session.execute(text("SELECT 1 FROM faces WHERE face_id = ANY(:fids) ORDER BY face_id FOR UPDATE"),
                        {"fids": list(face_ids)})


def _lock_faces_of_people(session: Session, person_ids: list[str]) -> None:
    """Lock every face named, rejected for, or shown as these people (_lock_faces)."""
    _lock_faces(session, list(session.execute(text(
        "SELECT face_id FROM face_person_matches WHERE person_id = ANY(:p)"
        " UNION SELECT face_id FROM face_person_rejections WHERE person_id = ANY(:p)"
        " UNION SELECT face_id FROM faces WHERE person_id = ANY(:p)"), {"p": person_ids}).scalars()))


def _mark_clusters_dirty(session: Session) -> None:
    """Say the face clusters changed (the cache is computed again on the next
    load). Triggers already say so for every change to which faces are
    clustered (migration d7f2a3b4c5e6); this is for what else regroups them
    (the faces' grouping settings)."""
    session.execute(text("SELECT mark_face_clusters_changed()"))


def regroup_faces(session: Session, before: dict, after: dict) -> None:
    """The faces producer's regroup: its grouping settings changed (not its
    level, which changes what's found next, not the groups)."""
    if any(before.get(k) != after.get(k) for k in ("merge_close_clusters", "merge_distance")):
        _mark_clusters_dirty(session)


def face_clusters_version(session: Session) -> str:
    """The last transaction that changed which faces are clustered: a cache
    computed at this version is current."""
    return session.execute(text(
        "SELECT value FROM system_metadata WHERE key = 'face_clusters_version'")).scalar() or "0"


#: Input-size cutoff between the two HDBSCAN regimes in
#: ``_cluster_face_embeddings``. Above this many face embeddings the helper
#: switches from EOM-with-single-cluster (right for tiny libraries) to leaf
#: selection (right for heterogeneous tails). Set at 50 because: a real
#: single-person library is unlikely to exceed ~50 detected faces, and
#: above 100 we know with certainty the input can't be one identity.
#: Anything in between is a judgment call; 50 is intentionally toward the
#: small side so the EOM fallback is reserved for genuinely tiny libraries.
LARGE_INPUT_THRESHOLD = 50


def face_merge_epsilon(session: Session) -> float:
    """How close (cosine distance) face clusters must be to merge, from the
    account's face settings (Settings → Processing → Faces): 0.0 when
    merging close groups is off."""
    from src.server.repository import lineage

    uses = lineage.uses(session, "faces")
    return float(uses["merge_distance"]) if uses["merge_close_clusters"] else 0.0


def _merge_close_clusters(
    dist: "np.ndarray", labels: "np.ndarray", epsilon: float, min_samples: int,
) -> "np.ndarray":
    """Leaf clusters closer than ``epsilon`` as one: what HDBSCAN's
    ``cluster_selection_epsilon`` does, done here because scikit-learn's
    epsilon search (1.8 and 1.9, ``_tree.pyx`` traverse_upwards) converts a
    one-element array to a scalar, which NumPy 2.4 refuses: it raises on
    every merge it would make.

    Two clusters merge when a chain of faces joins them with every step
    shorter than ``epsilon`` in HDBSCAN's own distance (mutual
    reachability: the larger of the two faces' distance and each one's
    distance to its ``min_samples``-th nearest face, itself counted, as
    scikit-learn reckons it). So a lone face between two people can't join
    them: it needs close neighbours of its own. Unlike scikit-learn's,
    faces left out of every cluster stay out: merging adds none.
    """
    import numpy as np
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    if epsilon <= 0 or labels.max(initial=-1) < 1:
        return labels
    core = np.partition(dist, min_samples - 1, axis=1)[:, min_samples - 1]
    dense = np.flatnonzero(core < epsilon)  # faces with close neighbours of their own
    if len(dense) == 0:
        return labels
    _, component = connected_components(csr_matrix(dist[np.ix_(dense, dense)] < epsilon), directed=False)

    parent = list(range(int(labels.max()) + 1))

    def root(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: dict[int, int] = {}  # component → a cluster in it
    for face, comp in zip(dense, component):
        label = int(labels[face])
        if label < 0:
            continue
        if comp in first:
            a, b = root(first[comp]), root(label)
            if a != b:
                parent[max(a, b)] = min(a, b)
        else:
            first[comp] = label
    return np.array([root(int(l)) if l >= 0 else -1 for l in labels])


def _cluster_face_embeddings(
    vectors: "np.ndarray",
    *,
    min_cluster_size: int = 3,
    merge_epsilon: float = 0.0,
) -> list[list[int]]:
    """Cluster L2-normalized face embeddings using HDBSCAN.

    Pure-math helper extracted from FaceRepository.compute_clusters so the
    clustering algorithm can be unit-tested without a database.

    See ``LARGE_INPUT_THRESHOLD`` for the input-size cutoff that decides
    which HDBSCAN regime applies.

    Runs HDBSCAN with metric='precomputed' over a cosine distance matrix.
    HDBSCAN is density-based, so a face must have at least ``min_cluster_size``
    close neighbors to anchor a cluster — this is what prevents the
    single-linkage chaining that an earlier union-find implementation
    suffered from (one bridging face would collapse two distinct identities).

    **Two regimes, switched on input size**:

    - **Small input (≤ ``LARGE_INPUT_THRESHOLD``).** Use the default EOM
      ('excess of mass') selection with ``allow_single_cluster=True``.
      This is the right call for tiny libraries — a user whose photos
      contain only one or two people genuinely has one density mode,
      and EOM-with-single-cluster surfaces it. Without this, EOM would
      label everything noise because no sub-cluster has excess mass
      relative to the root.

    - **Large input (> ``LARGE_INPUT_THRESHOLD``).** Switch to ``'leaf'``
      selection and disable single-cluster fallback. By the time the
      unassigned-face pool is in the hundreds, we *know* the result
      can't be one identity, but EOM+single-cluster will keep returning
      one giant cluster because the heterogeneous tail has no sharp
      density modes for it to find. Leaf selection walks the dendrogram
      and picks the densest leaf-level clusters instead — fragments
      heterogeneous tails into namable sub-groups. This is what
      diagnosed and fixed the "501-face mega-cluster" residue from a
      production library after most clusters had already been named.

      Leaf picks the finest clusters, so one person's sub-modes (years,
      glasses, light, angle) come back as separate groups of 5-15. With
      ``merge_epsilon`` > 0, leaf clusters closer than it merge
      (``_merge_close_clusters``); 0 is leaf alone. The small-input path
      and the fallback below don't merge.

    Args:
        vectors: (N, D) numpy array of L2-normalized face embeddings.
        min_cluster_size: HDBSCAN's core parameter. A cluster must contain
            at least this many faces or its members are labeled noise.
        merge_epsilon: cosine distance under which leaf clusters merge
            (large input only); 0.0 doesn't merge.

    Returns:
        Clusters as lists of row indices into ``vectors``, sorted by size
        descending. HDBSCAN noise points (label -1) are excluded.
    """
    import numpy as np
    from sklearn.cluster import HDBSCAN

    if len(vectors) < min_cluster_size:
        return []

    # Cosine distance matrix; with unit vectors this is 1 - dot(a, b).
    # Clamp to [0, 2] to defend against tiny FP overshoot (e.g. -1e-7),
    # which HDBSCAN's precomputed-metric path rejects.
    #
    # Float32 is sufficient for ArcFace cosine distances (the input embeddings
    # themselves are float32) and halves the matrix's memory footprint vs
    # float64 — at the max_faces=5000 cap that's ~50MB instead of ~200MB,
    # which matters because this function holds the full N×N matrix in RAM
    # while HDBSCAN runs.
    sim = vectors @ vectors.T
    dist = np.clip(1.0 - sim, 0.0, 2.0).astype(np.float32)
    np.fill_diagonal(dist, 0.0)  # precomputed metric requires exact zero diagonal

    is_large = len(vectors) > LARGE_INPUT_THRESHOLD

    if is_large:
        # Large input: try leaf selection first to actually fragment
        # heterogeneous tails. If leaf returns nothing (which happens for
        # a genuinely uniform large blob — e.g. a 100-photo library of
        # one person), fall back to EOM+single so the user still sees
        # their one cluster instead of an empty review screen.
        labels = HDBSCAN(
            metric="precomputed",
            min_cluster_size=min_cluster_size,
            copy=True,
            allow_single_cluster=False,
            cluster_selection_method="leaf",
        ).fit_predict(dist)
        # HDBSCAN's default min_samples is min_cluster_size.
        labels = _merge_close_clusters(dist, labels, merge_epsilon, min_samples=min_cluster_size)

        if not any(lbl >= 0 for lbl in labels):
            labels = HDBSCAN(
                metric="precomputed",
                min_cluster_size=min_cluster_size,
                copy=True,
                allow_single_cluster=True,
                cluster_selection_method="eom",
            ).fit_predict(dist)
    else:
        # Small input: original tiny-library path. EOM + single-cluster
        # surfaces a "this whole library is one person" result, which is
        # the legitimate use case the fallback exists for.
        labels = HDBSCAN(
            metric="precomputed",
            min_cluster_size=min_cluster_size,
            copy=True,
            allow_single_cluster=True,
            cluster_selection_method="eom",
        ).fit_predict(dist)

    from collections import defaultdict
    by_label: dict[int, list[int]] = defaultdict(list)
    for idx, lbl in enumerate(labels):
        if lbl < 0:  # noise
            continue
        by_label[int(lbl)].append(idx)

    clusters = list(by_label.values())
    clusters.sort(key=len, reverse=True)
    return clusters


def _bbox_corners(box: dict | None) -> tuple[float, float, float, float] | None:
    """Normalized face box as (x1, y1, x2, y2); accepts {x, y, w, h} or corners."""
    if not box:
        return None
    if "x1" in box:
        return box["x1"], box["y1"], box["x2"], box["y2"]
    if "x" in box:
        return box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
    return None


def _bbox_iou(a: dict | None, b: dict | None) -> float:
    """Intersection over union of two normalized face boxes (0.0 if either is missing)."""
    ca, cb = _bbox_corners(a), _bbox_corners(b)
    if ca is None or cb is None:
        return 0.0
    iw = min(ca[2], cb[2]) - max(ca[0], cb[0])
    ih = min(ca[3], cb[3]) - max(ca[1], cb[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (ca[2] - ca[0]) * (ca[3] - ca[1]) + (cb[2] - cb[0]) * (cb[3] - cb[1]) - inter
    return inter / union if union > 0 else 0.0


def current_face_model(session: Session) -> str:
    """The model that embeds faces now: the faces job's (Settings › AI,
    ADR-022), as the faces producer takes it; centroids, auto-assignment and
    clustering use only its embeddings. The account's is read once a session
    (whose it is: session.info["tenant_id"]); without one, the job's default."""
    from src.server.repository import lineage
    from src.server.repository.ai_machines import account_job_models

    if model := session.info.get("face_model"):
        return model
    tenant_id = session.info.get("tenant_id")
    models = account_job_models(tenant_id) if tenant_id else None
    model = str(lineage.desired(session, "faces", models)["settings"].get("model") or "buffalo_l")
    session.info["face_model"] = model
    return model


def _remove_keys(keys: list[str]) -> None:
    """Files of the previews folder whose rows are gone (wherever each is: a move may be under way)."""
    import logging

    from src.server.storage.store import get_store

    store = get_store()
    for key in keys:
        try:
            store.delete(key)
        except (OSError, ValueError) as exc:  # Unavailable: the daily sweep takes it; ValueError: never outside
            logging.getLogger(__name__).warning("Couldn't remove %s: %s", key, exc)


class FaceRepository:
    """CRUD for detected faces. Operates within a tenant session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_similar_by_vector(
        self,
        library_id: str,
        vector: list[float],
        limit: int,
        exclude_asset_id: str | None = None,
        embedding_model: str = "buffalo_l",
    ) -> list[tuple[str, float]]:
        """Find assets whose faces are nearest to the given face embedding
        (made by embedding_model: other models' faces can't be compared).

        Used by hybrid similarity search to retrieve identity matches.
        Multiple faces on the same asset are aggregated by best (lowest)
        cosine distance — we want one row per asset, not per face, so
        the downstream rank fusion sees a clean list. Filters to
        `availability = 'online'` and the requested library.

        Returns ``[(asset_id, distance)]`` sorted by distance ascending.
        """
        conditions = [
            "a.library_id = :library_id",
            "a.availability = 'online'",
            "f.embedding_vector IS NOT NULL",
            "f.embedding_model = :embedding_model",
        ]
        params: dict = {
            "vec": str(vector),
            "library_id": library_id,
            "limit": limit,
            "embedding_model": embedding_model,
        }
        if exclude_asset_id is not None:
            conditions.append("f.asset_id != :exclude_id")
            params["exclude_id"] = exclude_asset_id
        where = " AND ".join(conditions)
        sql = f"""
            SELECT
                f.asset_id,
                MIN(f.embedding_vector <=> CAST(:vec AS vector)) AS distance
            FROM faces f
            JOIN active_assets a ON a.asset_id = f.asset_id
            WHERE {where}
            GROUP BY f.asset_id
            ORDER BY distance ASC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql), params).fetchall()
        return [(r.asset_id, float(r.distance)) for r in rows]

    # Re-detection pairs each new detection with the old face it re-finds, so
    # the face keeps its face_id and everything a person attached to it
    # (confirmed assignment, dismissal, "not this person"). Overlapping boxes
    # pair only if the embeddings say it's the same face, and the closest
    # embedding wins: the person standing next to (or, in a mirrored photo,
    # in place of) a named face, even a lookalike sibling, must not inherit
    # its name. The same face seen by two detectors in the same photo is far
    # closer than 0.4; siblings are usually further. With both embeddings a
    # small overlap is enough (a tight Apple Vision box inside a loose
    # InsightFace one); without them, the floor is higher. Anything left
    # can still pair by embedding alone, under the same gate. Lookalikes
    # closer than 0.4 (identical twins) can still pair; the same face more
    # than 0.4 apart becomes a second face (nothing is lost).
    REDETECT_MIN_IOU = 0.1
    REDETECT_MIN_IOU_NO_EMBEDDING = 0.3
    REDETECT_MAX_EMBEDDING_DISTANCE = 0.4
    # Across a face-model switch the embeddings can't be compared: a name
    # carries over only where the boxes overlap substantially (Robert, Oct 9).
    REDETECT_MIN_IOU_ACROSS_SWITCH = 0.5

    def _dropped_crops(self, asset_id: str, face_ids: list[str]) -> list[str]:
        """The crops of these faces of the clip (the keys their rows hold)."""
        return list(self._session.execute(text(
            "SELECT crop_key FROM faces WHERE asset_id = :a AND face_id = ANY(:ids) AND crop_key IS NOT NULL"
        ), {"a": asset_id, "ids": face_ids}).scalars())

    def submit_faces(
        self,
        asset_id: str,
        detection_model: str,
        detection_model_version: str,
        faces: list[dict],
        embedding_model: str = "buffalo_l",
        *,
        after: AfterCommit | None = None,
    ) -> list[str]:
        """Store a fresh set of face detections for an asset and update face_count.
        embedding_model: which model embedded them (compared only within one model).
        after: the dropped faces' crops are removed once the caller has committed.

        A detection that re-finds an existing face (by box overlap, else by
        embedding) updates that face in place: it keeps its face_id, its
        confirmed person assignment and its rejections, while machine-made
        assignments are dropped and re-derived. Old faces that weren't
        re-found are deleted, unless a person confirmed them; those are kept
        as they are, since a re-run never drops human data. face_count is
        the new detections plus kept faces.

        Args:
            faces: list of dicts with keys: bounding_box, detection_confidence, embedding (optional),
                and for a video's, frame_ms and seen_ms (where in it the face was seen).

        Returns:
            One face_id per input detection, in input order.
        """
        from sqlalchemy import delete as sa_delete

        # The clip, then its faces (_lock_faces), before reading which a
        # person named or dismissed: one naming, dismissing or un-assigning
        # a face meanwhile is waited for and seen, not deleted with the face.
        self._session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset_id})
        _lock_faces(self._session, list(self._session.execute(
            text("SELECT face_id FROM faces WHERE asset_id = :a"), {"a": asset_id}).scalars()))
        old_rows = self._session.execute(
            text(
                "SELECT f.face_id, f.bounding_box_json, f.embedding_vector::text AS emb, f.embedding_model,"
                " EXISTS (SELECT 1 FROM face_person_matches m"
                "         WHERE m.face_id = f.face_id AND m.confirmed) AS confirmed,"
                " EXISTS (SELECT 1 FROM face_person_rejections r WHERE r.face_id = f.face_id) AS rejected"
                " FROM faces f WHERE f.asset_id = :aid"
            ),
            {"aid": asset_id},
        ).all()

        pairs = self._pair_redetected_faces(old_rows, faces, embedding_model)
        reused_ids = set(pairs.values())
        # A person's word on a face is kept even when it isn't found again:
        # a name (confirmed), or "not them" (a rejection), which a later
        # detection that finds it again must still honour.
        said = {r.face_id for r in old_rows if r.confirmed or r.rejected}
        kept_ids = [r.face_id for r in old_rows if r.face_id not in reused_ids and r.face_id in said]
        dropped_ids = [
            r.face_id for r in old_rows if r.face_id not in reused_ids and r.face_id not in said
        ]

        # Machine-made assignments on re-found faces are derived from the old
        # embedding; drop them (and the denormalized faces.person_id) so
        # auto-assign below re-derives them from the new one.
        unmatched_reused: list[str] = []
        if reused_ids:
            unmatched_reused = [
                row[0]
                for row in self._session.execute(
                    text(
                        "DELETE FROM face_person_matches"
                        " WHERE face_id = ANY(:fids) AND confirmed = false"
                        " RETURNING face_id"
                    ),
                    {"fids": list(reused_ids)},
                ).fetchall()
            ]
            if unmatched_reused:
                self._session.execute(
                    text("UPDATE faces SET person_id = NULL WHERE face_id = ANY(:fids)"),
                    {"fids": unmatched_reused},
                )

        affected_person_ids: list[str] = []
        if dropped_ids:
            self._session.execute(
                sa_delete(FacePersonMatch).where(FacePersonMatch.face_id.in_(dropped_ids))
            )
            # Capture the people whose representative is about to dangle
            # so we can re-pick a fresh face for each one after auto-
            # assign runs below. Without this, redetection silently
            # leaves named people with NULL representative_face_id and
            # their preview tile in the people grid goes blank — see
            # the lazy backfill in list_people / list_dismissed_people
            # for the read-side counterpart.
            affected_person_ids = [
                row[0]
                for row in self._session.execute(
                    text(
                        "SELECT person_id FROM people"
                        " WHERE representative_face_id = ANY(:fids)"
                    ),
                    {"fids": dropped_ids},
                ).fetchall()
            ]
            # Null out representative_face_id on people pointing to these faces
            self._session.execute(
                text(
                    "UPDATE people SET representative_face_id = NULL"
                    " WHERE representative_face_id = ANY(:fids)"
                ),
                {"fids": dropped_ids},
            )
            # Rejections on these faces go with them (ON DELETE CASCADE), and their crops after the commit.
            if after is not None:
                crops = self._dropped_crops(asset_id, dropped_ids)
                after.add("Removing dropped faces' crops", lambda: _remove_keys(crops))
            self._session.execute(sa_delete(Face).where(Face.face_id.in_(dropped_ids)))

        face_ids: list[str] = []
        for i, f in enumerate(faces):
            old_id = pairs.get(i)
            if old_id is not None:
                face = self._session.get(Face, old_id)
                face.bounding_box_json = f.get("bounding_box")
                face.embedding_vector = f.get("embedding")
                face.detection_confidence = f.get("detection_confidence")
                face.detection_model = detection_model
                face.detection_model_version = detection_model_version
                face.embedding_model = embedding_model
                face.frame_ms = f.get("frame_ms")
                face.seen_ms = f.get("seen_ms")
                self._session.add(face)
                face_ids.append(old_id)
                continue
            face_id = "face_" + str(ULID())
            face = Face(
                face_id=face_id,
                asset_id=asset_id,
                bounding_box_json=f.get("bounding_box"),
                embedding_vector=f.get("embedding"),
                detection_confidence=f.get("detection_confidence"),
                detection_model=detection_model,
                detection_model_version=detection_model_version,
                embedding_model=embedding_model,
                frame_ms=f.get("frame_ms"),
                seen_ms=f.get("seen_ms"),
            )
            self._session.add(face)
            face_ids.append(face_id)
        self._session.flush()

        # Update face_count on the asset
        self._session.execute(
            text("UPDATE assets SET face_count = :count WHERE asset_id = :aid"),
            {"count": len(faces) + len(kept_ids), "aid": asset_id},
        )

        # Confirmed faces found again by a new face model carry its embedding:
        # their people's centroids follow (this is how a name teaches the new
        # model what that person looks like). The same model's embedding
        # barely moves, so only a switch, a face's first embedding, or a
        # centroid still of another model recomputes; in person order, so two
        # workers can't deadlock.
        if reused_ids:
            # A switch, or a face given its first embedding (named on a Mac without the model).
            switched = [r.face_id for r in old_rows
                        if r.face_id in reused_ids
                        and (r.emb is None or (r.embedding_model or "buffalo_l") != embedding_model)]
            for (pid,) in self._session.execute(
                text("SELECT DISTINCT m.person_id FROM face_person_matches m"
                     " JOIN people p ON p.person_id = m.person_id"
                     " WHERE m.face_id = ANY(:fids) AND m.confirmed"
                     "   AND (m.face_id = ANY(:switched) OR p.centroid_model <> :model)"
                     " ORDER BY m.person_id"),
                {"fids": list(reused_ids), "switched": switched, "model": embedding_model},
            ).all():
                from src.server.repository.people import PersonRepository  # people imports this module

                PersonRepository(self._session)._recompute_centroid(pid)

        # Auto-assign the faces that don't carry a confirmed assignment.
        assigned = {
            row[0]
            for row in self._session.execute(
                text("SELECT face_id FROM face_person_matches WHERE face_id = ANY(:fids)"),
                {"fids": face_ids},
            ).fetchall()
        }
        unassigned = [(fid, f) for fid, f in zip(face_ids, faces) if fid not in assigned]
        if embedding_model == current_face_model(self._session):
            self._auto_assign_by_centroid(
                [fid for fid, _ in unassigned], [f for _, f in unassigned]
            )

        # A re-found face that lost its machine match and wasn't re-assigned
        # to the same person can't stay that person's representative.
        if unmatched_reused:
            affected_person_ids += [
                row[0]
                for row in self._session.execute(
                    text(
                        "SELECT p.person_id FROM people p"
                        " WHERE p.representative_face_id = ANY(:fids)"
                        "   AND NOT EXISTS (SELECT 1 FROM face_person_matches m"
                        "                   WHERE m.face_id = p.representative_face_id"
                        "                     AND m.person_id = p.person_id)"
                    ),
                    {"fids": unmatched_reused},
                ).fetchall()
            ]

        # Re-pick representative_face_id for any person whose previous
        # representative was deleted above. The auto-assign above will
        # often have re-attached new faces from this very asset back to
        # the same person, so the new representative is usually one of
        # the freshly inserted face_ids; if not, fall back to any other
        # face still attached to the person.
        for pid in affected_person_ids:
            new_rep = self._session.execute(
                text(
                    "SELECT f.face_id FROM faces f"
                    " JOIN face_person_matches m ON m.face_id = f.face_id"
                    " WHERE m.person_id = :pid"
                    " ORDER BY f.detection_confidence DESC NULLS LAST"
                    " LIMIT 1"
                ),
                {"pid": pid},
            ).scalar()
            # NULL when the person has no face left; list_people backfills lazily.
            self._session.execute(
                text(
                    "UPDATE people SET representative_face_id = :fid"
                    " WHERE person_id = :pid"
                ),
                {"fid": new_rep, "pid": pid},
            )

        _mark_clusters_dirty(self._session)
        self._session.commit()
        return face_ids

    @classmethod
    def _pair_redetected_faces(cls, old_rows: list, faces: list[dict],
                               embedding_model: str = "buffalo_l") -> dict[int, str]:
        """Pair new detections with the old faces they re-find.

        Embeddings are compared only when one model made both (a face-model
        switch puts them in different spaces, maybe of different sizes);
        across a switch the pair is judged on overlap alone, and only a
        substantial one (IoU >= REDETECT_MIN_IOU_ACROSS_SWITCH) counts.

        Greedy one-to-one. First overlapping boxes whose embeddings agree
        (distance < REDETECT_MAX_EMBEDDING_DISTANCE), closest embedding
        first and overlap as the tie-break; boxes without embeddings pair on
        overlap alone (IoU >= REDETECT_MIN_IOU_NO_EMBEDDING), after those.
        Then embedding distance alone, with the same gate, for whatever is
        left. Returns {index into faces: old face_id}.
        """
        import numpy as np

        def unit(values: list[float]) -> np.ndarray:
            vec = np.asarray(values, dtype=np.float32)
            norm = np.linalg.norm(vec)
            return vec / norm if norm > 0 else vec

        old_vecs = [
            unit([float(x) for x in row.emb.strip("[]").split(",")]) if row.emb else None
            for row in old_rows
        ]
        new_vecs = [unit(f["embedding"]) if f.get("embedding") else None for f in faces]

        def switched(oi: int) -> bool:
            return (getattr(old_rows[oi], "embedding_model", None) or "buffalo_l") != embedding_model

        def distance(oi: int, ni: int) -> float | None:
            if old_vecs[oi] is None or new_vecs[ni] is None:
                return None
            if switched(oi) or old_vecs[oi].shape != new_vecs[ni].shape:
                return None
            return 1.0 - float(old_vecs[oi] @ new_vecs[ni])

        pairs: dict[int, str] = {}
        used_old: set[int] = set()

        def take(candidates: list[tuple[float, int, int]]) -> None:
            for _, oi, ni in candidates:
                if oi in used_old or ni in pairs:
                    continue
                pairs[ni] = old_rows[oi].face_id
                used_old.add(oi)

        overlaps = []
        for oi, row in enumerate(old_rows):
            for ni, f in enumerate(faces):
                iou = _bbox_iou(row.bounding_box_json, f.get("bounding_box"))
                dist = distance(oi, ni)
                if dist is None:
                    ok = iou >= (cls.REDETECT_MIN_IOU_ACROSS_SWITCH if switched(oi)
                                 else cls.REDETECT_MIN_IOU_NO_EMBEDDING)
                else:
                    ok = iou >= cls.REDETECT_MIN_IOU and dist < cls.REDETECT_MAX_EMBEDDING_DISTANCE
                if ok:
                    rank = dist if dist is not None else cls.REDETECT_MAX_EMBEDDING_DISTANCE
                    overlaps.append((rank, -iou, oi, ni))
        take([(rank, oi, ni) for rank, _, oi, ni in sorted(overlaps)])

        close = []
        for oi in range(len(old_rows)):
            if oi in used_old:
                continue
            for ni in range(len(faces)):
                if ni in pairs:
                    continue
                dist = distance(oi, ni)
                if dist is not None and dist < cls.REDETECT_MAX_EMBEDDING_DISTANCE:
                    close.append((dist, oi, ni))
        take(sorted(close))
        return pairs

    def _rejections_for(self, face_ids: list[str]) -> dict[str, set[str]]:
        """Return {face_id: person_ids the user said it is not}."""
        if not face_ids:
            return {}
        rows = self._session.execute(
            text(
                "SELECT face_id, person_id FROM face_person_rejections"
                " WHERE face_id = ANY(:fids)"
            ),
            {"fids": face_ids},
        ).all()
        result: dict[str, set[str]] = {}
        for face_id, person_id in rows:
            result.setdefault(face_id, set()).add(person_id)
        return result

    # Auto-assign threshold — tighter than clustering (0.55) because centroids
    # are averaged over many confirmed faces and more stable.
    AUTO_ASSIGN_THRESHOLD = 0.45

    def _auto_assign_by_centroid(self, face_ids: list[str], faces_data: list[dict]) -> None:
        """Auto-assign new faces to known people if embedding is close to a centroid.

        Only assigns faces that have embeddings. Uses cosine distance against
        person centroid vectors, skipping people the user rejected for that
        face. Assigned with confirmed=false so user can review.
        """
        # Collect faces with embeddings
        faces_with_emb = [
            (fid, fd["embedding"])
            for fid, fd in zip(face_ids, faces_data)
            if fd.get("embedding")
        ]
        if not faces_with_emb:
            return

        # Get all people with centroids (of the current face model)
        rows = self._session.execute(
            text("SELECT person_id, centroid_vector::text FROM people"
                 " WHERE centroid_vector IS NOT NULL AND centroid_model = :model"),
            {"model": current_face_model(self._session)},
        ).all()
        if not rows:
            return

        import numpy as np

        # Parse centroids
        person_ids = [r[0] for r in rows]
        centroids = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in rows],
            dtype=np.float32,
        )
        # L2-normalize centroids
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        centroids = centroids / norms
        person_index = {pid: i for i, pid in enumerate(person_ids)}
        rejections = self._rejections_for([fid for fid, _ in faces_with_emb])

        for face_id, embedding in faces_with_emb:
            vec = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            # Cosine distance to each centroid
            distances = 1.0 - (centroids @ vec)
            for pid in rejections.get(face_id, ()):
                if pid in person_index:  # people without a centroid aren't candidates
                    distances[person_index[pid]] = np.inf
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])

            if best_dist < self.AUTO_ASSIGN_THRESHOLD:
                best_person_id = person_ids[best_idx]
                # Create match (confirmed=false for auto-assignment)
                match_id = "fpm_" + str(ULID())
                self._session.add(FacePersonMatch(
                    match_id=match_id,
                    face_id=face_id,
                    person_id=best_person_id,
                    confidence=1.0 - best_dist,
                    confirmed=False,
                ))
                # Sync denormalized column
                self._session.execute(
                    text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
                    {"pid": best_person_id, "fid": face_id},
                )
        self._session.flush()

    def propagate_assignments(self, batch_size: int = 5000) -> dict:
        """Scan unassigned faces and auto-assign to known people by centroid proximity.

        Returns {"assigned": N, "scanned": N}.
        Called by the upkeep timer to continuously improve tagging as users
        manually assign faces and centroids shift.
        """
        import numpy as np

        # Get all people with centroids, and below their unassigned faces: of
        # the current face model only (another model's can't be compared).
        model = current_face_model(self._session)
        people_rows = self._session.execute(
            text("SELECT person_id, centroid_vector::text FROM people"
                 " WHERE centroid_vector IS NOT NULL AND centroid_model = :model"),
            {"model": model},
        ).all()
        if not people_rows:
            return {"assigned": 0, "scanned": 0}

        person_ids = [r[0] for r in people_rows]
        centroids = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in people_rows],
            dtype=np.float32,
        )
        norms = np.linalg.norm(centroids, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        centroids = centroids / norms

        # Get unassigned faces with embeddings, excluding any face
        # whose owning asset is in the trash. Without the asset filter,
        # propagation would happily auto-assign trashed-asset faces to
        # named people, and then the user would see those people gain
        # face counts that point at 404'd thumbnails.
        rows = self._session.execute(
            text("""
                SELECT f.face_id, f.embedding_vector::text
                FROM faces f
                LEFT JOIN face_person_matches m ON m.face_id = f.face_id
                JOIN assets a ON a.asset_id = f.asset_id
                WHERE m.match_id IS NULL
                  AND f.embedding_vector IS NOT NULL
                  AND f.embedding_model = :model
                  AND a.deleted_at IS NULL
                ORDER BY f.detection_confidence DESC NULLS LAST
                LIMIT :limit
            """),
            {"limit": batch_size, "model": model},
        ).all()

        if not rows:
            return {"assigned": 0, "scanned": 0}

        # Never re-assign a face to a person the user removed it from.
        person_index = {pid: i for i, pid in enumerate(person_ids)}
        rejections = self._rejections_for([r[0] for r in rows])

        picks: dict[str, tuple[str, float]] = {}
        for face_id, emb_text in rows:
            vec = np.array([float(x) for x in emb_text.strip("[]").split(",")], dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            distances = 1.0 - (centroids @ vec)
            for pid in rejections.get(face_id, ()):
                if pid in person_index:  # people without a centroid aren't candidates
                    distances[person_index[pid]] = np.inf
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])

            if best_dist < self.AUTO_ASSIGN_THRESHOLD:
                picks[face_id] = (person_ids[best_idx], best_dist)

        # The faces first (_lock_faces); then only those still there and
        # unnamed, and not since rejected for their pick: a person's word wins.
        _lock_faces(self._session, list(picks))
        free = set(self._session.execute(
            text("SELECT f.face_id FROM faces f WHERE f.face_id = ANY(:fids)"
                 " AND NOT EXISTS (SELECT 1 FROM face_person_matches m WHERE m.face_id = f.face_id)"),
            {"fids": list(picks)},
        ).scalars()) if picks else set()
        rejections = self._rejections_for(list(free))
        assigned = 0
        for face_id, (best_person_id, best_dist) in picks.items():
            if face_id in free and best_person_id not in rejections.get(face_id, ()):
                self._session.add(FacePersonMatch(
                    match_id="fpm_" + str(ULID()),
                    face_id=face_id,
                    person_id=best_person_id,
                    confidence=1.0 - best_dist,
                    confirmed=False,
                ))
                self._session.execute(
                    text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
                    {"pid": best_person_id, "fid": face_id},
                )
                assigned += 1

        if assigned > 0:
            _mark_clusters_dirty(self._session)
        self._session.commit()  # and the locks go

        return {"assigned": assigned, "scanned": len(rows)}

    def set_crop_key(self, face_id: str, key: str) -> None:
        """Where the face's crop is stored (not committed)."""
        self._session.execute(text("UPDATE faces SET crop_key = :key WHERE face_id = :fid"), {"key": key, "fid": face_id})

    def crops_of(self, asset_ids: list[str]) -> list[tuple[str, str, str | None]]:
        """Every face of these clips: (asset_id, face_id, crop_key)."""
        return [tuple(r) for r in self._session.execute(text(
            "SELECT asset_id, face_id, crop_key FROM faces WHERE asset_id = ANY(:ids)"), {"ids": asset_ids}).all()]

    def people_in(self, asset_id: str) -> set[str]:
        """The people whose faces are in the clip."""
        return set(self._session.execute(text(
            "SELECT DISTINCT person_id FROM faces WHERE asset_id = :aid AND person_id IS NOT NULL"),
            {"aid": asset_id}).scalars().all())

    def clips_with(self, asset_ids: list[str], person_ids: list[str]) -> set[str]:
        """Which of these clips have a face of one of these people."""
        return set(self._session.execute(text(
            "SELECT DISTINCT asset_id FROM faces WHERE asset_id = ANY(:aids) AND person_id = ANY(:pids)"),
            {"aids": asset_ids, "pids": person_ids}).scalars().all())

    def crop_key_of(self, face_id: str) -> str | None:
        """Where the face's crop is stored, as the database says now."""
        return self._session.execute(text("SELECT crop_key FROM faces WHERE face_id = :f"), {"f": face_id}).scalar()

    def get(self, face_id: str) -> Face | None:
        return self._session.get(Face, face_id)

    def get_many(self, face_ids: list[str]) -> dict[str, Face]:
        return {f.face_id: f for f in self._session.exec(select(Face).where(Face.face_id.in_(face_ids))).all()}  # type: ignore[union-attr]

    def embeddings(self, face_ids: list[str], model: str) -> list[list[float]]:
        """The faces' embeddings in `model`'s space (those that have one)."""
        rows = self._session.execute(text(
            "SELECT embedding_vector::text FROM faces WHERE face_id = ANY(:fids) AND embedding_vector IS NOT NULL"
            " AND embedding_model = :model"), {"fids": face_ids, "model": model}).all()
        return [[float(x) for x in r[0].strip("[]").split(",")] for r in rows]

    def assigned_among(self, face_ids: list[str]) -> dict[str, str]:
        """Which of the faces are someone's (named or dismissed): face_id → person_id."""
        rows = self._session.execute(text("SELECT face_id, person_id FROM face_person_matches WHERE face_id = ANY(:fids)"),
                                     {"fids": face_ids}).all()
        return {r[0]: r[1] for r in rows}

    def lock_counting_assigned(self, face_ids: list[str]) -> int:
        """Locks the faces (in order: of two requests for them the second
        waits), then counts those someone's had since (named or dismissed)."""
        self._session.execute(text("SELECT 1 FROM faces WHERE face_id = ANY(:fids) ORDER BY face_id FOR UPDATE"),
                              {"fids": face_ids})
        return self._session.execute(text("SELECT count(*) FROM face_person_matches WHERE face_id = ANY(:fids)"),
                                     {"fids": face_ids}).scalar() or 0

    def get_by_asset_id(self, asset_id: str) -> list[Face]:
        """Return all faces for an asset, ordered by confidence desc."""
        stmt = (
            select(Face)
            .where(Face.asset_id == asset_id)
            .order_by(Face.detection_confidence.desc())  # type: ignore[union-attr]
        )
        return list(self._session.exec(stmt).all())

    def get_person_for_face(self, face_id: str) -> Person | None:
        """Return the person matched to a face, or None."""
        stmt = (
            select(Person)
            .join(FacePersonMatch, FacePersonMatch.person_id == Person.person_id)
            .where(FacePersonMatch.face_id == face_id)
        )
        return self._session.exec(stmt).first()

    def get_persons_for_faces(self, face_ids: list[str]) -> dict[str, Person]:
        """Return {face_id: Person} for all matched faces. Unmatched faces are absent."""
        if not face_ids:
            return {}
        stmt = (
            select(FacePersonMatch.face_id, Person)
            .join(Person, Person.person_id == FacePersonMatch.person_id)
            .where(FacePersonMatch.face_id.in_(face_ids))  # type: ignore[union-attr]
        )
        return {row[0]: row[1] for row in self._session.exec(stmt).all()}


    def compute_clusters(
        self,
        *,
        max_faces: int = 5000,
        min_cluster_size: int = 3,
        max_clusters: int | None = 20,
        faces_per_cluster: int = 6,
    ) -> tuple[list[list[dict]], list[list[str]], bool]:
        """Cluster unassigned face embeddings via HDBSCAN.

        Fetches all unassigned face embeddings, then runs density-based
        clustering on a cosine distance matrix. The earlier implementation
        was single-linkage union-find on a kNN graph, which catastrophically
        chained across identities (one observed cluster of 2887 / 2893 faces
        spanning all ages, genders, and ethnicities). HDBSCAN requires a
        density core to anchor a cluster, so a single bridging face can no
        longer collapse two distinct identities.

        Returns ``(clusters, all_face_ids, truncated)``:
            * ``clusters`` — list of cluster samples, each a list of face
              dicts truncated to ``faces_per_cluster``, sorted by
              detection_confidence desc.
            * ``all_face_ids`` — full per-cluster face ID list (used by the
              people router for naming a whole cluster at once).
            * ``truncated`` — True when the SQL hit the ``max_faces`` cap.
        """
        import numpy as np

        # Filter out faces whose underlying asset is in the trash —
        # otherwise the cluster review surfaces ghost clusters whose
        # lightbox 404s, and propagation would auto-assign them to
        # named people via centroid drift.
        sql = """
            SELECT f.face_id, f.embedding_vector::text
            FROM faces f
            LEFT JOIN face_person_matches m ON m.face_id = f.face_id
            JOIN assets a ON a.asset_id = f.asset_id
            WHERE m.match_id IS NULL
              AND f.embedding_vector IS NOT NULL
              AND f.embedding_model = :model
              AND a.deleted_at IS NULL
            ORDER BY f.detection_confidence DESC NULLS LAST
            LIMIT :max_faces
        """
        rows = self._session.execute(text(sql).bindparams(
            max_faces=max_faces, model=current_face_model(self._session))).all()
        truncated = len(rows) == max_faces

        if len(rows) < min_cluster_size:
            return [], [], truncated

        face_ids = [r[0] for r in rows]
        # Parse pgvector text format "[0.1,0.2,...]" → numpy array
        vectors = np.array(
            [[float(x) for x in r[1].strip("[]").split(",")] for r in rows],
            dtype=np.float32,
        )

        # L2-normalize defensively (ArcFace embeddings ship unit-length)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vectors = vectors / norms

        clusters_idx = _cluster_face_embeddings(
            vectors, min_cluster_size=min_cluster_size, merge_epsilon=face_merge_epsilon(self._session),
        )[:max_clusters]  # None: every cluster

        if not clusters_idx:
            return [], [], truncated

        clusters_face_ids = [[face_ids[i] for i in indices] for indices in clusters_idx]

        # Hydrate face + asset rows for sample display
        flat_ids = [fid for c in clusters_face_ids for fid in c]
        stmt = select(Face).where(Face.face_id.in_(flat_ids))  # type: ignore[union-attr]
        faces_by_id = {f.face_id: f for f in self._session.exec(stmt).all()}

        asset_ids = list({faces_by_id[fid].asset_id for fid in flat_ids if fid in faces_by_id})
        asset_map: dict[str, object] = {}
        if asset_ids:
            asset_stmt = select(Asset).where(Asset.asset_id.in_(asset_ids))  # type: ignore[union-attr]
            asset_map = {a.asset_id: a for a in self._session.exec(asset_stmt).all()}

        result: list[list[dict]] = []
        all_ids_per_cluster: list[list[str]] = []
        for cluster_face_ids in clusters_face_ids:
            cluster_faces = [faces_by_id[fid] for fid in cluster_face_ids if fid in faces_by_id]
            cluster_faces.sort(key=lambda f: f.detection_confidence or 0, reverse=True)
            all_ids_per_cluster.append([f.face_id for f in cluster_faces])

            sample = cluster_faces[:faces_per_cluster]
            cluster_data = []
            for f in sample:
                asset = asset_map.get(f.asset_id)
                cluster_data.append({
                    "face_id": f.face_id,
                    "asset_id": f.asset_id,
                    "bounding_box": f.bounding_box_json,
                    "detection_confidence": f.detection_confidence,
                    "rel_path": asset.rel_path if asset else None,
                    "media_type": asset.media_type if asset else None,
                    "frame_ms": f.frame_ms,  # a video's face: where in it (None: a photo's)
                })
            result.append(cluster_data)

        return result, all_ids_per_cluster, truncated

    def libraries_and_times(self, face_ids: list[str]) -> dict[str, tuple[str, datetime | None]]:
        """Each face's library and its clip's time (taken, else the file's;
        UTC), by face id: what a cluster shows of the faces a caller sees."""
        from datetime import UTC

        if not face_ids:
            return {}
        rows = self._session.execute(text(
            "SELECT f.face_id, a.library_id, COALESCE(a.taken_at, a.file_mtime) FROM faces f"
            " JOIN assets a ON a.asset_id = f.asset_id WHERE f.face_id = ANY(:ids)"), {"ids": list(face_ids)}).all()
        return {r[0]: (r[1], r[2].replace(tzinfo=UTC) if r[2] is not None and r[2].tzinfo is None else r[2])
                for r in rows}

    def cluster_cache(self, *, faces_per_cluster: int = 20) -> dict:
        """Every cluster of unassigned faces, as the cluster cache keeps them
        (``face_clusters_cache``): largest first, each with its position
        (``cluster_index``), size, a sample of faces, every face id (the
        routes name a cluster by them: people.cluster_id_of), and ``newest``, its latest photo (taken, else
        the file's time; ISO 8601, None when no photo says)."""
        from datetime import datetime, timezone

        clusters, all_ids, truncated = self.compute_clusters(max_clusters=None, faces_per_cluster=faces_per_cluster)
        flat = [fid for ids in all_ids for fid in ids]
        when: dict[str, datetime] = {}
        if flat:
            when = {r[0]: r[1] for r in self._session.execute(text(
                "SELECT f.face_id, COALESCE(a.taken_at, a.file_mtime) FROM faces f"
                " JOIN assets a ON a.asset_id = f.asset_id WHERE f.face_id = ANY(:ids)"), {"ids": flat}).all()
                if r[1] is not None}
        entries = []
        for i, (sample, ids) in enumerate(zip(clusters, all_ids)):
            dates = [when[fid] for fid in ids if fid in when]
            newest = max(dates) if dates else None
            if newest is not None and newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            entries.append({"cluster_index": i, "size": len(ids), "faces": sample, "face_ids": ids,
                            "newest": newest.isoformat() if newest else None})
        return {"clusters": entries, "truncated": truncated, "computed_at": datetime.now(timezone.utc).isoformat()}
