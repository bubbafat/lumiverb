"""People: named groups of faces. Every repository takes a Session."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlmodel import Session, select

from src.shared.utils import escape_like, utcnow
from src.server.models.tenant import (
    Face,
    FacePersonMatch,
    Person,
)
from ulid import ULID
from src.server.repository.faces import _lock_faces, _lock_faces_of_people, _mark_clusters_dirty, current_face_model


# A person's face in the libraries the caller sees (:within, NULL for every one), on faces f joined to assets a.
_WITHIN = "(CAST(:within AS text[]) IS NULL OR a.library_id = ANY(CAST(:within AS text[])))"
# The person (p) has a face in one of them, in sight or not.
_SEEN = ("(CAST(:within AS text[]) IS NULL OR EXISTS (SELECT 1 FROM face_person_matches sm"
         " JOIN faces sf ON sf.face_id = sm.face_id JOIN assets sa ON sa.asset_id = sf.asset_id"
         " WHERE sm.person_id = p.person_id AND sa.library_id = ANY(CAST(:within AS text[]))))")


class PersonRepository:
    """CRUD for people (tenant-scoped). Operates within a tenant session.

    within: the libraries the caller can see (ADR-018 decision 8, roles per
    library; None: every library). A person is shown when they have a face
    in one of those (shows), and with only those faces: counts, faces, the
    picture they're shown by."""

    def __init__(self, session: Session, within: list[str] | None = None) -> None:
        self._session = session
        self.within = None if within is None else list(within)

    def shows(self, person: Person) -> bool:
        """Whether the caller sees this person: a face of theirs is in a
        library the caller can see (every person when within is None)."""
        if self.within is None:
            return True
        return self._session.execute(text(f"SELECT {_SEEN} FROM people p WHERE p.person_id = :pid"),
                                     {"pid": person.person_id, "within": self.within}).scalar() is True

    def create(self, display_name: str, *, face_ids: list[str] | None = None) -> Person:
        """Create a new person. Optionally assign faces."""
        person_id = "person_" + str(ULID())
        person = Person(
            person_id=person_id,
            display_name=display_name,
            created_by_user=True,
        )
        self._session.add(person)
        self._session.flush()

        if face_ids:
            self._assign_faces(person_id, face_ids)
            self._recompute_centroid(person_id)
            # Set representative face = highest confidence
            rep = self._session.execute(
                text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST "
                    "LIMIT 1"
                ),
                {"pid": person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep

        if face_ids:
            _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(person)
        return person

    def create_dismissed(self, *, face_ids: list[str]) -> Person:
        """Create a dismissed person from a cluster. Faces are assigned but
        the person is hidden from the UI. Future similar faces are auto-absorbed."""
        person_id = "person_" + str(ULID())
        person = Person(
            person_id=person_id,
            display_name="(dismissed)",
            created_by_user=True,
            dismissed=True,
        )
        self._session.add(person)
        self._session.flush()

        if face_ids:
            self._assign_faces(person_id, face_ids)
            self._recompute_centroid(person_id)
            # Pick highest-confidence face as representative
            rep = self._session.execute(
                text(
                    "SELECT f.face_id FROM faces f "
                    "JOIN face_person_matches m ON m.face_id = f.face_id "
                    "WHERE m.person_id = :pid "
                    "ORDER BY f.detection_confidence DESC NULLS LAST "
                    "LIMIT 1"
                ),
                {"pid": person_id},
            ).scalar()
            if rep:
                person.representative_face_id = rep

        self._session.commit()
        self._session.refresh(person)
        return person

    def get_by_id(self, person_id: str) -> Person | None:
        return self._session.get(Person, person_id)

    def list_with_face_counts(
        self,
        *,
        after: str | None = None,
        limit: int = 50,
        q: str | None = None,
    ) -> list[tuple[Person, int]]:
        """Return people with *visible* face counts, sorted by count desc.

        Trashed-asset faces are excluded from the count via a LEFT JOIN
        on the assets table — a person whose only photos are in the
        trash gets count 0 instead of their pre-trash count, matching
        what they'd see in the detail view. People with zero visible
        faces still appear in the list (it's a LEFT JOIN, not an INNER).

        Cursor is base64-encoded JSON {"count": N, "id": person_id}.
        Optional q filters by display_name (case-insensitive substring match).
        """
        import base64 as _b64

        having_conditions: list[str] = []
        params: dict[str, object] = {"limit": limit, "within": self.within}

        if q:
            having_conditions.append(
                "LOWER(p.display_name) LIKE '%' || LOWER(:q) || '%' ESCAPE '\\'"
            )
            params["q"] = escape_like(q)

        if after:
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_count = decoded["count"]
                cursor_id = decoded["id"]
                # Cursor count refers to the visible-faces count (the
                # COUNT expression in the SELECT). Use the alias rather
                # than re-computing the COUNT(...) inline.
                having_conditions.append(
                    "(COUNT(a.asset_id)::int < :cursor_count"
                    "  OR (COUNT(a.asset_id)::int = :cursor_count"
                    "      AND p.person_id > :cursor_id))"
                )
                params["cursor_count"] = cursor_count
                params["cursor_id"] = cursor_id
            except Exception:
                pass

        having_sql = (" HAVING " + " AND ".join(having_conditions)) if having_conditions else ""

        # The visible-faces count is COUNT(a.asset_id): the chained
        # LEFT JOIN to assets with `deleted_at IS NULL` in the ON clause
        # produces a row per match where the asset is NOT trashed, and
        # NULL otherwise. COUNT skips NULLs, so this gives us exactly
        # the visible count. People with zero visible faces still come
        # out of the GROUP BY because the people row is preserved.
        sql = f"""
            SELECT p.person_id, p.display_name, p.created_by_user,
                   p.representative_face_id, p.confirmation_count, p.created_at,
                   COUNT(a.asset_id)::int AS cnt
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            LEFT JOIN faces f ON f.face_id = m.face_id
            LEFT JOIN assets a
                ON a.asset_id = f.asset_id AND a.deleted_at IS NULL AND {_WITHIN}
            WHERE p.dismissed = false AND {_SEEN}
            GROUP BY p.person_id
            {having_sql}
            ORDER BY cnt DESC, p.person_id ASC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()
        result = []
        for row in rows:
            person = self.get_by_id(row.person_id)
            if person:
                result.append((person, row.cnt))
        return result

    def list_dismissed(
        self,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[tuple[Person, int]]:
        """Return dismissed people with *visible* face counts.

        Same trashed-asset filtering as ``list_with_face_counts`` — see
        the comment there for the COUNT(a.asset_id) pattern.
        """
        import base64 as _b64

        having_conditions: list[str] = []
        params: dict[str, object] = {"limit": limit, "within": self.within}

        if after:
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_count = decoded["count"]
                cursor_id = decoded["id"]
                having_conditions.append(
                    "(COUNT(a.asset_id)::int < :cursor_count"
                    "  OR (COUNT(a.asset_id)::int = :cursor_count"
                    "      AND p.person_id > :cursor_id))"
                )
                params["cursor_count"] = cursor_count
                params["cursor_id"] = cursor_id
            except Exception:
                pass

        having_sql = (" HAVING " + " AND ".join(having_conditions)) if having_conditions else ""

        sql = f"""
            SELECT p.person_id, p.display_name, p.created_by_user,
                   p.representative_face_id, p.confirmation_count, p.created_at,
                   COUNT(a.asset_id)::int AS cnt
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            LEFT JOIN faces f ON f.face_id = m.face_id
            LEFT JOIN assets a
                ON a.asset_id = f.asset_id AND a.deleted_at IS NULL AND {_WITHIN}
            WHERE p.dismissed = true AND {_SEEN}
            GROUP BY p.person_id
            {having_sql}
            ORDER BY cnt DESC, p.person_id ASC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()
        result = []
        for row in rows:
            person = self.get_by_id(row.person_id)
            if person:
                result.append((person, row.cnt))
        return result

    def cleanup_empty_dismissed(self) -> int:
        """Delete dismissed people that have zero face matches (none assigned,
        by a person or the machine). Never one a person named.

        Returns the number of dismissed people deleted.
        """
        empty_ids = [
            row[0]
            for row in self._session.execute(
                text("""
                    SELECT p.person_id
                    FROM people p
                    LEFT JOIN face_person_matches m ON m.person_id = p.person_id
                    WHERE p.dismissed = true
                      AND p.display_name = '(dismissed)'
                    GROUP BY p.person_id
                    HAVING COUNT(m.match_id) = 0
                """)
            ).fetchall()
        ]
        if not empty_ids:
            return 0

        self._session.execute(
            text("UPDATE faces SET person_id = NULL WHERE person_id = ANY(:pids)"),
            {"pids": empty_ids},
        )
        self._session.execute(
            text("DELETE FROM people WHERE person_id = ANY(:pids)"),
            {"pids": empty_ids},
        )
        self._session.commit()
        return len(empty_ids)

    def best_face(self, person_id: str, *, seen: bool = False) -> str | None:
        """The person's most confidently detected face: a representative for
        one without; seen, of those in the libraries within."""
        return self._session.execute(text(
            "SELECT f.face_id FROM faces f "
            "JOIN face_person_matches m ON m.face_id = f.face_id "
            "JOIN assets a ON a.asset_id = f.asset_id "
            f"WHERE m.person_id = :pid AND {_WITHIN if seen else 'TRUE'} "
            "ORDER BY f.detection_confidence DESC NULLS LAST LIMIT 1"),
            {"pid": person_id, "within": self.within}).scalar()

    def ensure_representative(self, person: Person) -> None:
        """Gives a person without a representative face their best one, and
        commits: one orphaned by re-detection (submit_faces deletes an asset's
        old face rows and inserts new ones with new ids) before submit_faces
        picked another."""
        if person.representative_face_id:
            return
        if face_id := self.best_face(person.person_id):
            person.representative_face_id = face_id
            self._session.add(person)
            self._session.commit()

    def representative_asset_id(self, person: Person) -> str | None:
        """The clip the person's representative face is in (their thumbnail's)."""
        face_id = self.representative_face_id(person)
        if not face_id:
            return None
        face = self._session.get(Face, face_id)
        return face.asset_id if face else None

    def representative_face_id(self, person: Person) -> str | None:
        """The face the person is shown by: their representative, unless it's
        in a library the caller can't see, then their best face the caller can."""
        face_id = person.representative_face_id
        if self.within is None or face_id is None:
            return face_id
        in_sight = self._session.execute(text(
            f"SELECT 1 FROM faces f JOIN assets a ON a.asset_id = f.asset_id WHERE f.face_id = :f AND {_WITHIN}"),
            {"f": face_id, "within": self.within}).first() is not None
        return face_id if in_sight else self.best_face(person.person_id, seen=True)

    def undismiss(self, person: Person, display_name: str) -> None:
        """A dismissed person named and shown again (with a representative face
        if they had none); commits."""
        person.dismissed = False
        person.display_name = display_name
        if not person.representative_face_id and (face_id := self.best_face(person.person_id)):
            person.representative_face_id = face_id
        self._session.add(person)
        _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(person)

    def assign_to(self, person_id: str, face_ids: list[str]) -> None:
        """Faces a person named (a cluster's) given to someone already named,
        their centroid made again; commits."""
        self._assign_faces(person_id, face_ids)
        self._recompute_centroid(person_id)
        _mark_clusters_dirty(self._session)
        self._session.commit()

    def named_centroids(self, model: str, exclude_id: str | None = None) -> list[tuple[str, str, list[float], int]]:
        """Every named (not dismissed) person with a centroid in `model`'s
        space: (person_id, display_name, centroid, face_count)."""
        rows = self._session.execute(text(f"""
            SELECT p.person_id, p.display_name, p.centroid_vector::text,
                   COUNT(a.asset_id)::int AS face_count
            FROM people p
            LEFT JOIN face_person_matches m ON m.person_id = p.person_id
            LEFT JOIN faces f ON f.face_id = m.face_id
            LEFT JOIN assets a ON a.asset_id = f.asset_id AND {_WITHIN}
            WHERE p.dismissed = false AND p.centroid_vector IS NOT NULL
                  AND p.centroid_model = :model
                  AND p.person_id IS DISTINCT FROM CAST(:exclude_id AS text)
                  AND {_SEEN}
            GROUP BY p.person_id
        """), {"model": model, "exclude_id": exclude_id, "within": self.within}).all()
        return [(r[0], r[1], [float(x) for x in r[2].strip("[]").split(",")], r[3]) for r in rows]

    def get_face_count(self, person_id: str) -> int:
        """Return the number of *visible* faces matched to a person.

        Trashed assets (``deleted_at IS NOT NULL``) are excluded so the
        count agrees with what ``get_faces`` returns and what the user
        sees in the people grid / detail view.
        """
        result = self._session.execute(
            text(
                "SELECT COUNT(*)::int"
                " FROM face_person_matches m"
                " JOIN faces f ON f.face_id = m.face_id"
                " JOIN assets a ON a.asset_id = f.asset_id"
                f" WHERE m.person_id = :pid AND a.deleted_at IS NULL AND {_WITHIN}"
            ),
            {"pid": person_id, "within": self.within},
        ).scalar()
        return result or 0

    def update_name(self, person_id: str, display_name: str) -> Person | None:
        person = self.get_by_id(person_id)
        if person is None:
            return None
        person.display_name = display_name
        self._session.add(person)
        self._session.commit()
        self._session.refresh(person)
        return person

    def delete(self, person_id: str) -> bool:
        """Delete a person and all their face matches."""
        person = self.get_by_id(person_id)
        if person is None:
            return False
        _lock_faces_of_people(self._session, [person_id])
        # Clear denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = NULL WHERE person_id = :pid"),
            {"pid": person_id},
        )
        # Delete matches
        self._session.execute(
            text("DELETE FROM face_person_matches WHERE person_id = :pid"),
            {"pid": person_id},
        )
        self._session.delete(person)
        _mark_clusters_dirty(self._session)
        self._session.commit()
        return True

    def get_faces(
        self,
        person_id: str,
        *,
        after: str | None = None,
        limit: int = 50,
    ) -> list[Face]:
        """Return faces matched to a person, cursor-paginated by face_id.

        Filters out faces whose underlying asset is in the trash
        (``deleted_at IS NOT NULL``). The ``face_person_matches`` rows
        are kept intact so untrashing the asset restores the assignment
        automatically — only the read query hides them.
        """
        conditions = ["m.person_id = :pid", "a.deleted_at IS NULL", _WITHIN]
        params: dict[str, object] = {"pid": person_id, "limit": limit, "within": self.within}

        if after:
            conditions.append("f.face_id > :after")
            params["after"] = after

        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT f.face_id
            FROM faces f
            JOIN face_person_matches m ON m.face_id = f.face_id
            JOIN assets a ON a.asset_id = f.asset_id
            WHERE {where_sql}
            ORDER BY f.face_id ASC
            LIMIT :limit
        """
        face_ids = [row[0] for row in self._session.execute(text(sql).bindparams(**params)).all()]
        if not face_ids:
            return []
        stmt = select(Face).where(Face.face_id.in_(face_ids))  # type: ignore[union-attr]
        faces_by_id = {f.face_id: f for f in self._session.exec(stmt).all()}
        return [faces_by_id[fid] for fid in face_ids if fid in faces_by_id]

    def assign_face(self, face_id: str, person_id: str, *, confidence: float | None = None, confirmed: bool = False) -> FacePersonMatch:
        """Assign a face to a person. Raises if face is already assigned (unique constraint).

        An explicit assignment overrides an earlier rejection of the same pair.
        """
        _lock_faces(self._session, [face_id])
        self._session.execute(
            text("DELETE FROM face_person_rejections WHERE face_id = :fid AND person_id = :pid"),
            {"fid": face_id, "pid": person_id},
        )
        match_id = "fpm_" + str(ULID())
        match = FacePersonMatch(
            match_id=match_id,
            face_id=face_id,
            person_id=person_id,
            confidence=confidence,
            confirmed=confirmed,
            confirmed_at=utcnow() if confirmed else None,
        )
        self._session.add(match)
        # Sync denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = :pid WHERE face_id = :fid"),
            {"pid": person_id, "fid": face_id},
        )
        self._recompute_centroid(person_id)
        _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(match)
        return match

    def unassign_face(self, face_id: str) -> bool:
        """Remove face-person assignment and clear denormalized faces.person_id.

        Records a rejection ("this face is not this person") so neither
        auto-assignment nor the upkeep sweep puts it back.
        """
        _lock_faces(self._session, [face_id])
        # Get the person_id before deleting (for centroid recomputation)
        old_pid = self._session.execute(
            text("SELECT person_id FROM face_person_matches WHERE face_id = :fid"),
            {"fid": face_id},
        ).scalar()
        result = self._session.execute(
            text("DELETE FROM face_person_matches WHERE face_id = :fid"),
            {"fid": face_id},
        )
        if result.rowcount > 0:  # type: ignore[union-attr]
            self._session.execute(
                text("UPDATE faces SET person_id = NULL WHERE face_id = :fid"),
                {"fid": face_id},
            )
            if old_pid:
                self._session.execute(
                    text(
                        "INSERT INTO face_person_rejections (face_id, person_id, created_at)"
                        " VALUES (:fid, :pid, :now) ON CONFLICT DO NOTHING"
                    ),
                    {"fid": face_id, "pid": old_pid, "now": utcnow()},
                )
                # The person's tile can't keep showing a face that isn't theirs.
                rep_id = self._session.execute(
                    text("SELECT representative_face_id FROM people WHERE person_id = :pid"),
                    {"pid": old_pid},
                ).scalar()
                if rep_id == face_id:
                    self._repick_representative(old_pid)
            if old_pid:
                self._recompute_centroid(old_pid)
            _mark_clusters_dirty(self._session)
            self._session.commit()
            return True
        return False

    def _repick_representative(self, person_id: str) -> None:
        """Point the person's tile at their best remaining face, or NULL if none (no commit)."""
        best = self._session.execute(
            text(
                "SELECT f.face_id FROM faces f "
                "JOIN face_person_matches m ON m.face_id = f.face_id "
                "WHERE m.person_id = :pid "
                "ORDER BY f.detection_confidence DESC NULLS LAST "
                "LIMIT 1"
            ),
            {"pid": person_id},
        ).scalar()
        self._session.execute(
            text("UPDATE people SET representative_face_id = :fid WHERE person_id = :pid"),
            {"fid": best, "pid": person_id},
        )

    def _assign_faces(self, person_id: str, face_ids: list[str]) -> None:
        """Batch assign faces to a person (no commit).

        These come from a person's action (naming, dismissing or assigning a
        cluster), so they're confirmed and survive re-detection. Faces the
        user already rejected for this person are skipped: a bulk action
        doesn't override a per-face decision.
        """
        _lock_faces(self._session, face_ids)
        rejected = {
            row[0]
            for row in self._session.execute(
                text(
                    "SELECT face_id FROM face_person_rejections"
                    " WHERE person_id = :pid AND face_id = ANY(:fids)"
                ),
                {"pid": person_id, "fids": face_ids},
            ).fetchall()
        } if face_ids else set()
        face_ids = [fid for fid in face_ids if fid not in rejected]
        now = utcnow()
        for fid in face_ids:
            match_id = "fpm_" + str(ULID())
            self._session.add(FacePersonMatch(
                match_id=match_id,
                face_id=fid,
                person_id=person_id,
                confirmed=True,
                confirmed_at=now,
            ))
        self._session.flush()
        # Sync denormalized faces.person_id
        if face_ids:
            self._session.execute(
                text("UPDATE faces SET person_id = :pid WHERE face_id = ANY(:fids)"),
                {"pid": person_id, "fids": face_ids},
            )
            _mark_clusters_dirty(self._session)

    def _recompute_centroid(self, person_id: str) -> None:
        """Recompute centroid_vector as mean of all matched *visible* face embeddings.

        Trashed-asset faces are excluded from the average so the
        centroid doesn't drift toward photos the user has thrown away
        — that drift would feed back into ``propagate_assignments`` and
        snowball more trashed-asset matches into the same person.
        """
        # Only the current face model's embeddings: another model's live in
        # another space. With none yet (mid-switch), no centroid: nothing is
        # auto-assigned to them until a face of theirs is found again.
        model = current_face_model(self._session)
        self._session.execute(
            text("""
                UPDATE people SET centroid_vector = sub.avg_vec, centroid_model = :model
                FROM (
                    SELECT AVG(f.embedding_vector) AS avg_vec
                    FROM faces f
                    JOIN face_person_matches m ON m.face_id = f.face_id
                    JOIN assets a ON a.asset_id = f.asset_id
                    WHERE m.person_id = :pid
                      AND f.embedding_vector IS NOT NULL
                      AND f.embedding_model = :model
                      AND a.deleted_at IS NULL
                ) sub
                WHERE person_id = :pid
            """),
            {"pid": person_id, "model": model},
        )
        # Update confirmation_count
        count = self._session.execute(
            text("SELECT COUNT(*)::int FROM face_person_matches WHERE person_id = :pid AND confirmed = TRUE"),
            {"pid": person_id},
        ).scalar() or 0
        self._session.execute(
            text("UPDATE people SET confirmation_count = :cnt WHERE person_id = :pid"),
            {"cnt": count, "pid": person_id},
        )

    def merge(self, target_person_id: str, source_person_id: str) -> Person | None:
        """Merge source person into target. Returns updated target, or None if either not found.

        Atomic: reassign all matches from source to target, update faces.person_id,
        recompute centroid, pick best representative, delete source.
        Uses SELECT ... FOR UPDATE on source to serialize concurrent merges.
        """
        _lock_faces_of_people(self._session, [target_person_id, source_person_id])
        # Lock source to serialize concurrent merges
        source = self._session.execute(
            text("SELECT person_id FROM people WHERE person_id = :pid FOR UPDATE"),
            {"pid": source_person_id},
        ).first()
        if source is None:
            return None

        target = self.get_by_id(target_person_id)
        if target is None:
            return None

        # S and T are one person, so "not S" means "not T". When both people
        # carry a rejection for a face, keep the later one.
        self._session.execute(
            text(
                "INSERT INTO face_person_rejections (face_id, person_id, created_at)"
                " SELECT face_id, :tid, created_at FROM face_person_rejections"
                " WHERE person_id = :sid"
                " ON CONFLICT (face_id, person_id) DO UPDATE"
                " SET created_at = GREATEST(face_person_rejections.created_at, EXCLUDED.created_at)"
            ),
            {"tid": target_person_id, "sid": source_person_id},
        )
        # Reassign face_person_matches from source to target
        self._session.execute(
            text("UPDATE face_person_matches SET person_id = :tid WHERE person_id = :sid"),
            {"tid": target_person_id, "sid": source_person_id},
        )
        # Update denormalized faces.person_id
        self._session.execute(
            text("UPDATE faces SET person_id = :tid WHERE person_id = :sid"),
            {"tid": target_person_id, "sid": source_person_id},
        )
        # A face now both matched to and rejected for the merged person: a
        # machine match always loses to the user's "not this person"; between
        # a confirmation and a rejection, the later decision wins. This is
        # the same whichever way the two people are merged.
        unassigned = [
            row[0]
            for row in self._session.execute(
                text(
                    "DELETE FROM face_person_matches m USING face_person_rejections r"
                    " WHERE m.person_id = :tid AND r.person_id = :tid AND r.face_id = m.face_id"
                    "   AND (NOT m.confirmed"
                    "        OR COALESCE(m.confirmed_at, m.created_at) < r.created_at)"
                    " RETURNING m.face_id"
                ),
                {"tid": target_person_id},
            ).fetchall()
        ]
        if unassigned:
            self._session.execute(
                text("UPDATE faces SET person_id = NULL WHERE face_id = ANY(:fids)"),
                {"fids": unassigned},
            )
        self._session.execute(
            text(
                "DELETE FROM face_person_rejections r USING face_person_matches m"
                " WHERE r.person_id = :tid AND m.person_id = :tid AND m.face_id = r.face_id"
            ),
            {"tid": target_person_id},
        )

        # Recompute centroid on target
        self._recompute_centroid(target_person_id)

        # Pick best representative face from merged set
        best_face_id = self._session.execute(
            text(
                "SELECT f.face_id FROM faces f "
                "JOIN face_person_matches m ON m.face_id = f.face_id "
                "WHERE m.person_id = :pid "
                "ORDER BY f.detection_confidence DESC NULLS LAST "
                "LIMIT 1"
            ),
            {"pid": target_person_id},
        ).scalar()
        # NULL when the merge left the person with no faces.
        target.representative_face_id = best_face_id

        # Delete source person
        self._session.execute(
            text("DELETE FROM people WHERE person_id = :pid"),
            {"pid": source_person_id},
        )

        _mark_clusters_dirty(self._session)
        self._session.commit()
        self._session.refresh(target)
        return target
