"""Libraries, their path filters, and the changes reported to them. Every repository takes a Session."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.sql import text as sa_text
from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.tenant import (
    Asset,
    Library,
    LibraryPathFilter,
    TenantPathFilterDefault,
)
from ulid import ULID



class LibraryChangeRepository:
    """Paths reported changed on storage, waiting for the server to scan them."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, library_id: str, rel_paths: list[str]) -> None:
        """Add paths, or bump the version and time of ones already pending."""
        for rel_path in dict.fromkeys(rel_paths):
            self._session.execute(
                sa_text(
                    "INSERT INTO library_changes (change_id, library_id, rel_path, reported_at)"
                    " VALUES (:change_id, :library_id, :rel_path, clock_timestamp())"
                    " ON CONFLICT (library_id, rel_path) DO UPDATE"
                    " SET reported_at = clock_timestamp(),"
                    "     version = nextval('library_changes_version_seq')"
                ),
                {"change_id": "chg_" + str(ULID()), "library_id": library_id, "rel_path": rel_path},
            )
        self._session.commit()

    def pending(self, library_id: str, limit: int) -> tuple[list[dict], bool]:
        """Oldest first. Returns (changes, truncated)."""
        rows = self._session.execute(
            sa_text(
                "SELECT change_id, rel_path, reported_at, version FROM library_changes"
                " WHERE library_id = :library_id ORDER BY reported_at, change_id LIMIT :limit"
            ),
            {"library_id": library_id, "limit": limit + 1},
        ).all()
        return [dict(r._mapping) for r in rows[:limit]], len(rows) > limit

    def acknowledge(self, library_id: str, seen: list[tuple[str, int]]) -> int:
        """Remove changes whose version is the one the scan saw. Returns how many."""
        removed = 0
        for change_id, version in seen:
            result = self._session.execute(
                sa_text(
                    "DELETE FROM library_changes"
                    " WHERE library_id = :library_id AND change_id = :change_id AND version <= :version"
                ),
                {"library_id": library_id, "change_id": change_id, "version": version},
            )
            removed += result.rowcount or 0
        self._session.commit()
        return removed

    def summary(self) -> list[dict]:
        rows = self._session.execute(
            sa_text(
                "SELECT library_id, COUNT(*) AS pending, MIN(reported_at) AS oldest_reported_at"
                " FROM library_changes GROUP BY library_id ORDER BY library_id"
            )
        ).all()
        return [dict(r._mapping) for r in rows]


class LibraryRepository:
    """Repository for libraries table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str, root_path: str, reader: str = "server", *, private: bool = False,
               infer_location: bool = False) -> Library:
        """Generate library_id as lib_ + ULID(), insert, return Library."""
        library_id = "lib_" + str(ULID())
        library = Library(
            library_id=library_id,
            name=name,
            root_path=root_path,
            reader=reader,
            private=private,
            infer_location=infer_location,
        )
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def bump_revision(self, library_id: str) -> None:
        """Atomically increment the library revision counter.

        Uses a separate short transaction to avoid holding a row lock
        on the libraries table for the duration of long ingest transactions.
        """
        engine = self._session.get_bind()
        with engine.connect() as conn:
            conn.execute(
                text(
                    "UPDATE libraries SET revision = revision + 1,"
                    " last_scan_at = now() WHERE library_id = :lid"
                ),
                {"lid": library_id},
            )
            conn.commit()

    def get_by_id(self, library_id: str) -> Library | None:
        """Return library by id or None."""
        stmt = select(Library).where(Library.library_id == library_id)
        return self._session.exec(stmt).first()

    def update(self, library: Library, *, name: str | None = None, root_path: str | None = None,
               is_public: bool | None = None, reader: str | None = None, private: bool | None = None,
               infer_location: bool | None = None) -> None:
        """What's given changes (None: as it is); commits."""
        if name is not None:
            library.name = name
        if private is not None:
            library.private = private
        if infer_location is not None:
            library.infer_location = infer_location
        if root_path is not None:
            library.root_path = root_path
        if reader is not None:
            library.reader = reader
        if is_public is not None:
            library.is_public = is_public
        library.updated_at = utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)

    def pending_counts(self) -> dict[str, int]:
        """Per library not in the trash: its clips with work still to do
        (no preview, or anything a producer still needs, failures waiting
        their turn included). A library with no clips isn't listed."""
        from src.server.repository.assets import FACE_EMBEDDINGS, OUTSTANDING_CONDITIONS
        from src.server.repository.lineage import lineage_join

        pending = "a.proxy_key IS NULL OR " + " OR ".join(
            f"({cond})" for cond in (*OUTSTANDING_CONDITIONS.values(), FACE_EMBEDDINGS))
        rows = self._session.execute(text(f"""
            SELECT a.library_id, COUNT(*) FILTER (WHERE {pending}) AS pending
            FROM active_assets a
            {lineage_join()}
            JOIN libraries l ON l.library_id = a.library_id
            WHERE l.status != 'trashed'
            GROUP BY a.library_id
        """)).all()
        return {row.library_id: row.pending for row in rows}

    def active_brief(self) -> list[tuple[str, str, str]]:
        """The active libraries' (library_id, name, reader), by name."""
        return [tuple(r) for r in self._session.execute(text(
            "SELECT library_id, name, reader FROM libraries WHERE status = 'active' ORDER BY name")).all()]

    def roots_of(self, library_ids: set[str]) -> dict[str, str]:
        """Each library's root folder."""
        if not library_ids:
            return {}
        return {lib.library_id: lib.root_path for lib in self._session.exec(
            select(Library).where(Library.library_id.in_(library_ids))).all()}  # type: ignore[attr-defined]

    def get_by_name(self, name: str) -> Library | None:
        """Return library by name or None."""
        stmt = select(Library).where(Library.name == name)
        return self._session.exec(stmt).first()

    def list_all(self, include_trashed: bool = False) -> list[Library]:
        """Return all libraries. By default exclude status='trashed'; if include_trashed=True return all."""
        stmt = select(Library)
        if not include_trashed:
            stmt = stmt.where(Library.status != "trashed")
        return list(self._session.exec(stmt).all())

    def get_trashed(self) -> list[Library]:
        """Return all libraries with status='trashed'."""
        stmt = select(Library).where(Library.status == "trashed")
        return list(self._session.exec(stmt).all())

    def trash(self, library_id: str) -> Library:
        """Set library status to trashed, soft-delete all its assets, return updated library."""
        library = self.get_by_id(library_id)
        if library is None:
            raise ValueError(f"Library not found: {library_id}")
        if library.status == "trashed":
            raise ValueError(f"Library already trashed: {library_id}")
        # Soft-delete all assets in this library. The reason says the clip
        # went with its library, not that its file went missing.
        self._session.execute(
            text(
                "UPDATE assets SET deleted_at = :now, deleted_reason = 'library'"
                " WHERE library_id = :library_id AND deleted_at IS NULL"
            ),
            {"library_id": library_id, "now": utcnow()},
        )
        library.status = "trashed"
        library.is_public = False
        library.trashed_at = library.updated_at = utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def lock_trashed(self, library_id: str, before: datetime | None = None) -> bool:
        """Lock the library until the transaction ends if it's still in the trash
        (since before `before`): what deleting it for good re-checks, and what
        restoring it waits for."""
        return self._session.execute(
            text("SELECT 1 FROM libraries WHERE library_id = :lib AND status = 'trashed'"
                 "   AND (CAST(:before AS timestamptz) IS NULL OR trashed_at < :before) FOR UPDATE"),
            {"lib": library_id, "before": before},
        ).first() is not None

    def restore(self, library_id: str) -> Library:
        """Take a library out of the trash with the clips that went with it.
        Clips trashed or archived before it went stay as they were. Waits for
        a purge that has it locked; then it's gone (ValueError)."""
        if not self.lock_trashed(library_id):
            self._session.rollback()
            raise ValueError(f"Library not in the trash: {library_id}")
        library = self.get_by_id(library_id)
        if library is None:
            raise ValueError(f"Library not in the trash: {library_id}")
        self._session.refresh(library)
        # A clip whose path another clip holds now (a file ingested there as
        # the library went) stays out of sight, archived as missing where it
        # was: a different file at a path is a new clip. Once more if an
        # ingest takes a path between the two.
        from sqlalchemy.exc import IntegrityError

        for attempt in range(3):
            try:
                with self._session.begin_nested():
                    self._session.execute(
                        text(
                            "UPDATE assets a SET deleted_reason = 'missing' WHERE a.library_id = :lib"
                            "   AND a.deleted_reason = 'library' AND EXISTS (SELECT 1 FROM assets o WHERE"
                            "       o.library_id = a.library_id AND o.rel_path = a.rel_path AND o.deleted_at IS NULL)"
                        ),
                        {"lib": library_id},
                    )
                    self._session.execute(
                        text(
                            "UPDATE assets SET deleted_at = NULL, deleted_reason = NULL, trashed_from = NULL,"
                            " search_synced_at = NULL WHERE library_id = :lib AND deleted_reason = 'library'"
                        ),
                        {"lib": library_id},
                    )
                break
            except IntegrityError:
                if attempt == 2:
                    raise
        # Clips a person trashed before the library went were out of the trash's
        # sight with it: back in the trash, they get their full trash days again.
        self._session.execute(
            text("UPDATE assets SET deleted_at = :now WHERE library_id = :lib AND deleted_reason = 'user'"),
            {"lib": library_id, "now": utcnow()},
        )
        library.status = "active"
        library.is_public = False  # its public page doesn't come back without a person saying so
        library.trashed_at = None
        library.updated_at = utcnow()
        self._session.add(library)
        self._session.commit()
        self._session.refresh(library)
        return library

    def hard_delete(self, library_id: str) -> None:
        """Permanently delete library and all related data in FK-safe order. Single transaction."""
        # Order: asset_metadata, video_scenes, video_index_chunks, assets, libraries
        params = {"library_id": library_id}
        self._session.execute(
            text(
                """
                DELETE FROM asset_metadata
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM asset_embeddings
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM video_scenes
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM video_index_chunks
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        # Face tables: matches → null out people refs → faces
        self._session.execute(
            text(
                """
                DELETE FROM face_person_matches
                WHERE face_id IN (
                    SELECT face_id FROM faces
                    WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                )
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                UPDATE people SET representative_face_id = NULL
                WHERE representative_face_id IN (
                    SELECT face_id FROM faces
                    WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                )
                """
            ),
            params,
        )
        self._session.execute(
            text(
                """
                DELETE FROM faces
                WHERE asset_id IN (SELECT asset_id FROM assets WHERE library_id = :library_id)
                """
            ),
            params,
        )
        self._session.execute(text("DELETE FROM assets WHERE library_id = :library_id"), params)
        self._session.execute(
            text("DELETE FROM library_path_filters WHERE library_id = :library_id"), params
        )
        self._session.execute(text("DELETE FROM libraries WHERE library_id = :library_id"), params)
        from src.server.repository.visits import VisitRepository

        VisitRepository(self._session).forget("library", [library_id])  # its visits (ADR-025)
        self._session.commit()

    def resolve_cover(self, library: Library) -> str | None:
        """The effective cover: the chosen one while it's in sight, else the
        most recent clip by taken_at. A chosen cover whose file went missing
        (or that's archived) is the cover again when it's back; one that's
        gone from the library is cleared. (A person trashing it clears the
        choice in trash_many, as for projects.)"""
        if library.cover_asset_id:
            row = self._session.execute(
                select(Asset.asset_id, Asset.deleted_at).where(
                    Asset.asset_id == library.cover_asset_id,
                    Asset.library_id == library.library_id,
                )
            ).first()
            if row and row[1] is None:
                return library.cover_asset_id
            if row is None:  # gone for good: clear it (lazy self-healing)
                library.cover_asset_id = None
                library.updated_at = utcnow()
                self._session.add(library)
                self._session.commit()

        # Fallback: most recent active asset by taken_at
        row = self._session.execute(
            select(Asset.asset_id).where(
                Asset.library_id == library.library_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(Asset.taken_at.desc().nullslast())  # type: ignore[union-attr]
            .limit(1)
        ).first()
        return row[0] if row else None


class PathFilterRepository:
    """Repository for library_path_filters and tenant_path_filter_defaults."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_library(self, library_id: str) -> list[LibraryPathFilter]:
        """Return all path filters for a library."""
        stmt = select(LibraryPathFilter).where(LibraryPathFilter.library_id == library_id)
        return list(self._session.exec(stmt).all())

    def add_for_library(self, library_id: str, type: str, pattern: str) -> LibraryPathFilter:
        """Add a filter for a library. filter_id = lpf_ + ULID."""
        filter_id = "lpf_" + str(ULID())
        row = LibraryPathFilter(
            filter_id=filter_id,
            library_id=library_id,
            type=type,
            pattern=pattern,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete_for_library(self, filter_id: str, library_id: str) -> bool:
        """Delete a filter by id and library_id. Returns False if not found."""
        stmt = select(LibraryPathFilter).where(
            LibraryPathFilter.filter_id == filter_id,
            LibraryPathFilter.library_id == library_id,
        )
        row = self._session.exec(stmt).first()
        if row is None:
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    def list_defaults(self, tenant_id: str) -> list[TenantPathFilterDefault]:
        """Return all tenant path filter defaults."""
        stmt = select(TenantPathFilterDefault).where(
            TenantPathFilterDefault.tenant_id == tenant_id
        )
        return list(self._session.exec(stmt).all())

    def add_default(self, tenant_id: str, type: str, pattern: str) -> TenantPathFilterDefault:
        """Add a tenant default. default_id = tpfd_ + ULID."""
        default_id = "tpfd_" + str(ULID())
        row = TenantPathFilterDefault(
            default_id=default_id,
            tenant_id=tenant_id,
            type=type,
            pattern=pattern,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete_default(self, default_id: str, tenant_id: str) -> bool:
        """Delete a tenant default by id and tenant_id. Returns False if not found."""
        stmt = select(TenantPathFilterDefault).where(
            TenantPathFilterDefault.default_id == default_id,
            TenantPathFilterDefault.tenant_id == tenant_id,
        )
        row = self._session.exec(stmt).first()
        if row is None:
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    def copy_defaults_to_library(self, tenant_id: str, library_id: str) -> int:
        """Copy current tenant defaults into library_path_filters. Returns count copied. New filter_id ULIDs are generated."""
        defaults = self.list_defaults(tenant_id)
        if not defaults:
            return 0
        count = 0
        for d in defaults:
            self.add_for_library(library_id=library_id, type=d.type, pattern=d.pattern)
            count += 1
        return count
