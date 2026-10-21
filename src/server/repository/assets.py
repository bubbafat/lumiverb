"""Clips (the assets table and its active_assets view) and what's saved with each: the text read in
it, a machine's description and tags, its vectors. What each producer still needs
(MISSING_CONDITIONS) is listed from here. Every repository takes a Session."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import and_, bindparam, column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.sql import text as sa_text
from sqlmodel import Session, select

from src.producers.live import LiveMap
from src.shared.io_utils import normalize_path_prefix, normalize_rel_path
from src.shared.utils import escape_like, utcnow
from src.shared import asset_status
from src.server.models.similarity import SimilarityScope
from src.server.models.tenant import (
    Asset,
    AssetEmbedding,
    AssetMetadata,
    Library,
    VideoFacetRow,
)
from ulid import ULID
# Canonical view for non-trashed assets. Use in raw SQL (e.g. FROM active_assets).
ACTIVE_ASSETS = "active_assets"

# What each producer the scheduler runs still needs, by its name (the
# repair summary counts it), from the reconciler (repository/lineage.py):
# missing, or made from a file whose content has since changed, less
# failures waiting their turn. OUTSTANDING includes those failures (the
# library health dot). Both as the producers are now (src/producers/live.py).
# FACE_EMBEDDINGS: faces found without their vector.
FACE_EMBEDDINGS = (
    "a.face_count > 0 AND a.media_type = 'image'"
    " AND EXISTS (SELECT 1 FROM faces f WHERE f.asset_id = a.asset_id AND f.embedding_vector IS NULL)"
)


def _reconciled(rule: str) -> dict[str, str]:
    from src.server.repository import lineage
    from src.shared.producers import SCHEDULED

    of = getattr(lineage, rule)
    return {artifact: of(artifact) for artifact in SCHEDULED}


MISSING_CONDITIONS: Mapping[str, str] = LiveMap(lambda: _reconciled("due"))
OUTSTANDING_CONDITIONS: Mapping[str, str] = LiveMap(lambda: _reconciled("outstanding"))


def _active_assets_subquery():
    # active_assets is a DB view, not a SQLModel table.
    return (
        sa_text("SELECT asset_id, library_id, rel_path FROM active_assets")
        .columns(
            column("asset_id"),
            column("library_id"),
            column("rel_path"),
        )
        .subquery("active_a")
    )

def _missing_file():
    """Archived because a scan no longer finds the file: it comes back when the
    file does. Not a person's archive, not the trash."""
    return and_(Asset.deleted_at.is_not(None), Asset.deleted_reason == "missing")


def _under(folder: str | None) -> str | None:
    """A LIKE pattern for every path under the folder, its name taken literally
    (escape character backslash); None for the whole library."""
    folder = normalize_path_prefix(folder)
    if folder is None:
        return None
    return escape_like(folder) + "/%"


def _split(asked: list[str], changed: set[str]) -> tuple[list[str], list[str]]:
    """(changed, the rest), each in the order asked."""
    return [a for a in asked if a in changed], [a for a in asked if a not in changed]


class AssetRepository:
    """Repository for assets table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_library_and_rel_path(self, library_id: str, rel_path: str) -> Asset | None:
        """The clip at (library_id, rel_path): the one in sight, else the most
        recently archived or trashed there (a changed file is a new clip, so
        a path can have archived clips besides), or None."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id, Asset.rel_path == rel_path)
            .order_by(Asset.deleted_at.is_(None).desc(), Asset.deleted_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        return self._session.exec(stmt).first()

    def create_asset(
        self,
        library_id: str,
        rel_path: str,
        file_size: int,
        file_mtime: datetime | None,
        media_type: str,
    ) -> Asset:
        """Create a new asset."""
        asset_id = "ast_" + str(ULID())
        asset = Asset(
            asset_id=asset_id,
            library_id=library_id,
            rel_path=rel_path,
            file_size=file_size,
            file_mtime=file_mtime,
            media_type=media_type,
            status=asset_status.PENDING,
            availability="online",
        )
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def get_by_id(self, asset_id: str) -> Asset | None:
        """Return active (non-trashed) asset by id or None."""
        stmt = select(Asset).where(Asset.asset_id == asset_id).where(Asset.deleted_at.is_(None))
        return self._session.exec(stmt).first()

    def path_date_media(self, asset_ids: list[str]) -> dict[str, tuple]:
        """Each clip's (rel_path, taken_at, media_type), trashed or not: what a face list shows of it."""
        if not asset_ids:
            return {}
        rows = self._session.exec(select(Asset.asset_id, Asset.rel_path, Asset.taken_at, Asset.media_type)
                                  .where(Asset.asset_id.in_(asset_ids))).all()  # type: ignore[union-attr]
        return {r[0]: tuple(r[1:]) for r in rows}

    def existing_ids(self, asset_ids: list[str]) -> set[str]:
        """Which of these clips exist, in sight or not."""
        if not asset_ids:
            return set()
        return set(self._session.execute(text("SELECT asset_id FROM assets WHERE asset_id = ANY(:ids)"),
                                         {"ids": asset_ids}).scalars())

    def in_sight(self, asset_ids: list[str]) -> set[str]:
        """Which of these clips are in sight (not trashed or archived)."""
        if not asset_ids:
            return set()
        return set(self._session.execute(text("SELECT asset_id FROM active_assets WHERE asset_id = ANY(:ids)"),
                                         {"ids": list(asset_ids)}).scalars())

    def unsynced_count(self) -> int:
        """Clips in sight that search doesn't have as they are now."""
        return self._session.execute(text(
            "SELECT count(*) FROM active_assets WHERE search_synced_at IS NULL")).scalar() or 0

    def video_facets(self, asset_ids: list[str]) -> dict[str, VideoFacetRow]:
        """Each video's facets (duration, codecs, …): those it has."""
        if not asset_ids:
            return {}
        return {f.asset_id: f for f in self._session.exec(
            select(VideoFacetRow).where(VideoFacetRow.asset_id.in_(asset_ids))).all()}  # type: ignore[attr-defined]

    def get_any(self, asset_id: str) -> Asset | None:
        """The clip in sight or not (trashed, archived): get_by_id leaves those out."""
        return self._session.get(Asset, asset_id)

    def lock_for_update(self, asset: Asset) -> None:
        """One write of the clip at a time, with it as it is now (read again under the lock)."""
        self._session.execute(text("SELECT 1 FROM assets WHERE asset_id = :a FOR UPDATE"), {"a": asset.asset_id})
        self._session.refresh(asset)

    def missing_share(self, asset_ids: list[str]) -> list:
        """For each library these clips (in sight) are in: how many of them
        (going) and how many clips it has in sight."""
        return self._session.execute(text("""
            SELECT a.library_id,
                   COUNT(*) FILTER (WHERE a.asset_id = ANY(:ids))::int AS going,
                   COUNT(*)::int AS in_sight
            FROM assets a
            WHERE a.deleted_at IS NULL
              AND a.library_id IN (SELECT library_id FROM assets WHERE asset_id = ANY(:ids) AND deleted_at IS NULL)
            GROUP BY a.library_id
        """), {"ids": asset_ids}).all()

    def repair_counts(self, library_id: str) -> dict:
        """A library's clips, and what's missing by name (ADR-024): what
        scans left out (previews, EXIF: lumiverb.preview, lumiverb.metadata)
        and what each producer still needs (MISSING_CONDITIONS); faces
        without their vector, stale search entries, and clip-steps left out
        of those because their last try failed and their turn hasn't come
        (waiting_failures)."""
        from src.producers.names import METADATA, PREVIEW, sql_spelling
        from src.server.repository import lineage
        from src.server.search.sync import STALE_SEARCH  # the search sweep's own rule
        from src.shared.producers import SCHEDULED

        counts = ",\n".join(f"COUNT(*) FILTER (WHERE {cond}) AS {sql_spelling(name)}"
                             for name, cond in MISSING_CONDITIONS.items())
        # waiting() first: cheap, and false for nearly every clip, so the rest is rarely looked at.
        waiting = " + ".join(f"COUNT(*) FILTER (WHERE {lineage.waiting(a)} AND {lineage.outstanding(a)})"
                             for a in SCHEDULED)
        row = self._session.execute(text(f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE proxy_key IS NULL) AS missing_preview,
                COUNT(*) FILTER (WHERE exif_extracted_at IS NULL AND media_type = 'image') AS missing_metadata,
                {counts},
                COUNT(*) FILTER (WHERE {FACE_EMBEDDINGS}) AS faces_without_vectors,
                COUNT(*) FILTER (WHERE {STALE_SEARCH}) AS stale_search_sync,
                {waiting} AS waiting_failures
            FROM active_assets a
            {lineage.lineage_join()}
            WHERE library_id = :library_id
        """), {"library_id": library_id}).mappings().one()
        return {"total": row["total"], "faces_without_vectors": row["faces_without_vectors"],
                "stale_search_sync": row["stale_search_sync"], "waiting_failures": row["waiting_failures"],
                "missing": {PREVIEW: row["missing_preview"], METADATA: row["missing_metadata"],
                            **{name: row[sql_spelling(name)] for name in MISSING_CONDITIONS}}}

    def mark_unsynced(self, asset: Asset) -> None:
        """Stale until search has the clip as it is now (not committed)."""
        asset.search_synced_at = None
        self._session.add(asset)

    def set_transcript(self, asset: Asset, *, srt: str | None, text: str | None, language: str | None,
                       source: str, has_transcript: bool) -> None:
        """The clip's transcript, replacing any (not committed)."""
        asset.transcript_srt = srt
        asset.transcript_text = text
        asset.transcript_language = language
        asset.transcript_source = source
        asset.transcribed_at = utcnow()
        asset.has_transcript = has_transcript
        asset.updated_at = utcnow()
        self._session.add(asset)

    def clear_transcript(self, asset: Asset, *, has_transcript: bool | None) -> None:
        """No transcript shown: has_transcript None, missing again (made
        again); False, none to make (not committed)."""
        asset.transcript_srt = None
        asset.transcript_text = None
        asset.transcript_language = None
        asset.transcript_source = None
        asset.transcribed_at = None
        asset.has_transcript = has_transcript
        asset.updated_at = utcnow()
        self._session.add(asset)

    def set_note(self, asset: Asset, note: str | None, author: str | None) -> None:
        """A person's note on the clip, or none (not committed)."""
        asset.note = note
        asset.note_author = author if note else None
        asset.note_updated_at = utcnow() if note else None
        asset.updated_at = utcnow()
        self._session.add(asset)

    def file_seen_again(self, asset: Asset, *, file_size: int, file_mtime: datetime | None, media_type: str) -> None:
        """Its file as ingest found it now; one marked missing that came back
        is in sight again (search finds it again: its words were kept). Not
        committed."""
        asset.file_size = file_size
        if file_mtime is not None:
            asset.file_mtime = file_mtime
        asset.media_type = media_type
        if asset.deleted_at is not None:
            self.clear_trash(asset)
        self._session.add(asset)

    def folder_has_clips(self, library_ids: list[str], folder: str) -> bool:
        """Clips in sight under this folder of these libraries."""
        return self._session.execute(text(
            "SELECT 1 FROM active_assets WHERE library_id = ANY(:libs) AND starts_with(rel_path, :dir) LIMIT 1"),
            {"libs": list(library_ids), "dir": folder.strip("/") + "/"}).first() is not None

    # -- A handover: an archived clip moved onto its empty copy (routers/trash.py) ----------

    def lock_timeout(self) -> str:
        return self._session.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()

    def set_lock_timeout(self, value: str) -> None:
        """For the rest of the transaction."""
        self._session.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": value})

    def move_onto_copy(self, archived: Asset, copy: Asset) -> None:
        """The archived clip takes its copy's path and file, in sight again; the
        copy moved out of the way first (one clip in sight holds a path) and
        marked handed over; a cover someone set to the copy follows the clip.
        Flushed, not committed."""
        path = copy.rel_path
        copy.rel_path = f".lumiverb-handed-over/{copy.asset_id}"
        copy.deleted_at = utcnow()
        copy.deleted_reason = "handed_over"
        self._session.add(copy)
        self._session.flush()
        archived.rel_path = path
        archived.file_size = copy.file_size
        archived.file_mtime = copy.file_mtime
        archived.updated_at = utcnow()
        self.clear_trash(archived)
        self._session.add(archived)
        for table in ("libraries", "projects"):
            self._session.execute(text(f"UPDATE {table} SET cover_asset_id = :a WHERE cover_asset_id = :c"),
                                  {"a": archived.asset_id, "c": copy.asset_id})
        self._session.flush()

    def list_pending_by_library(self, library_id: str) -> list[Asset]:
        """Return all active (non-trashed) assets in library with status='pending'."""
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id)
            .where(Asset.status == "pending")
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def list_ids_matching_pattern(self, library_id: str, pattern: str) -> list[str]:
        """Return asset_ids of active assets whose rel_path matches a glob pattern."""
        from src.shared.path_filter import _glob_match

        stmt = (
            select(Asset.asset_id, Asset.rel_path)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
        )
        return [
            row[0] for row in self._session.execute(stmt).all()
            if _glob_match(pattern, row[1])
        ]

    def count_by_library(self, library_id: str) -> int:
        """Return total active asset count for library."""
        result = self._session.execute(
            text(
                "SELECT COUNT(*)::int FROM active_assets WHERE library_id = :library_id"
            ),
            {"library_id": library_id},
        )
        return int(result.scalar() or 0)

    def count_all_for_libraries(self, library_ids: list[str]) -> int:
        """Return total active asset count across all given libraries in a single query."""
        if not library_ids:
            return 0
        stmt = text(
            "SELECT COUNT(*)::int FROM active_assets WHERE library_id IN :library_ids"
        ).bindparams(bindparam("library_ids", expanding=True))
        result = self._session.execute(stmt, {"library_ids": library_ids})
        return int(result.scalar() or 0)

    def list_rel_paths_for_library_non_deleted(self, library_id: str) -> list[str]:
        """Return rel_path for all active (non-trashed) assets in library."""
        stmt = (
            select(Asset.rel_path)
            .where(Asset.library_id == library_id)
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())

    def trash(self, asset_id: str, *, reason: str = "missing") -> bool:
        """One clip out of sight; see trash_many. False if nothing changed."""
        trashed, _ = self.trash_many([asset_id], reason=reason)
        return bool(trashed)

    def trash_many(self, asset_ids: list[str], *, reason: str = "missing",
                   commit: bool = True) -> tuple[list[str], list[str]]:
        """Take clips out of sight with this reason. Returns (changed, unchanged), in the order given.

        "missing" (a scan no longer finds the file) takes only clips in sight.
        "user" (a person's trash) takes archived clips too, missing or archived
        by hand: deleting an archived clip moves it to the trash, and its
        trash days count from now. A clip trashed with its library stays
        with the library, and comes back with it.

        commit=False: the caller commits, with what else must go with it (a
        scan's finish: the handovers to copies and the scan's close).
        """
        if not asset_ids:
            return [], []
        result = self._session.execute(
            text(
                """
                UPDATE assets SET deleted_at = :now, deleted_reason = :reason,
                    -- what restoring it from the trash goes back to: the archive, or sight
                    trashed_from = CASE WHEN deleted_at IS NOT NULL THEN deleted_reason END
                WHERE asset_id = ANY(:ids)
                  AND (deleted_at IS NULL
                       OR (CAST(:reason AS text) = 'user' AND deleted_reason IN ('missing', 'archived')))
                RETURNING asset_id, trashed_from
                """
            ),
            {"now": utcnow(), "reason": reason, "ids": asset_ids},
        )
        rows = result.fetchall()
        changed = {row[0] for row in rows}
        # Those that were in sight: guesses made from them are checked again (ADR-017).
        from src.server.repository import locations

        locations.fixes_gone(self._session, [row[0] for row in rows if row[1] is None])
        if reason == "user" and changed:
            # A person deleting a project's or library's chosen cover clip
            # clears the choice: the first active clip shows until a new one
            # is chosen (Robert, Oct 9), and restoring it doesn't bring it back.
            self._session.execute(text("UPDATE projects SET cover_asset_id = NULL WHERE cover_asset_id = ANY(:ids)"),
                                  {"ids": list(changed)})
            self._session.execute(text("UPDATE libraries SET cover_asset_id = NULL WHERE cover_asset_id = ANY(:ids)"),
                                  {"ids": list(changed)})
        if commit:
            self._session.commit()
        return _split(asset_ids, changed)

    def trashable(self, asset_ids: list[str]) -> list[str]:
        """Of these, the clips a person's trash would take: in sight, or archived."""
        if not asset_ids:
            return []
        return list(self._session.execute(
            text("SELECT asset_id FROM assets WHERE asset_id = ANY(:ids)"
                 " AND (deleted_at IS NULL OR deleted_reason IN ('missing', 'archived'))"),
            {"ids": asset_ids},
        ).scalars().all())

    def restore(self, asset_id: str) -> str:
        """Take one clip out of the trash. Returns "restored", "not_found",
        "not_trashed", "library_trashed" (it comes back with its library),
        "path_taken" (another clip in sight is at its path now), or the clip's
        archive reason ("archived" or "missing"): an archive isn't the trash.
        See restore_many."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            return "not_found"
        if asset.deleted_at is None:
            return "not_trashed"
        if asset.deleted_reason in ("archived", "missing"):
            return asset.deleted_reason
        if asset.deleted_reason == "library":
            return "library_trashed"
        if asset.deleted_reason != "user":
            return "not_trashed"
        restored, _, _ = self.restore_many([asset_id])
        if restored:
            return "restored"
        held = self._session.execute(text(
            "SELECT 1 FROM assets o WHERE o.library_id = :lib AND o.rel_path = :p AND o.deleted_at IS NULL"
            " AND o.asset_id <> :a"), {"lib": asset.library_id, "p": asset.rel_path, "a": asset_id}).first()
        return "path_taken" if held else "library_trashed"

    _IN_A_LIVE_LIBRARY = (
        " AND NOT EXISTS (SELECT 1 FROM libraries l WHERE l.library_id = a.library_id AND l.status = 'trashed')"
    )

    def restore_many(self, asset_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
        """Take clips a person trashed out of the trash, back to where they were:
        in sight, or the archive when they were archived before (as a project
        comes back active or archived). Not those whose library is in the
        trash: they come back with it. Returns (restored, skipped, to_archive),
        to_archive being the restored ones that went back to the archive.

        Search finds those back in sight at once: their words, scenes and
        transcript segments were kept (ADR-019).
        """
        if not asset_ids:
            return [], [], []
        result = self._session.execute(
            text(
                "UPDATE assets a SET deleted_reason = a.trashed_from, deleted_at = :now, trashed_from = NULL"
                " WHERE a.asset_id = ANY(:ids) AND a.deleted_reason = 'user'"
                "   AND a.trashed_from IN ('archived', 'missing')" + self._IN_A_LIVE_LIBRARY +
                " RETURNING a.asset_id"
            ),
            {"ids": asset_ids, "now": utcnow()},
        )
        to_archive = {row[0] for row in result.fetchall()}
        back, _ = self._bring_back(asset_ids, "a.deleted_reason = 'user'" + self._IN_A_LIVE_LIBRARY)
        restored = to_archive | set(back)
        return ([a for a in asset_ids if a in restored], [a for a in asset_ids if a not in restored],
                [a for a in asset_ids if a in to_archive])

    def archive(self, asset_ids: list[str]) -> tuple[list[str], list[str]]:
        """A person archives clips in sight: out of sight, kept forever, and
        scans leave them archived. Returns (archived, skipped)."""
        if not asset_ids:
            return [], []
        result = self._session.execute(
            text(
                "UPDATE assets SET deleted_at = :now, deleted_reason = 'archived'"
                " WHERE asset_id = ANY(:ids) AND deleted_at IS NULL RETURNING asset_id"
            ),
            {"now": utcnow(), "ids": asset_ids},
        )
        archived = {row[0] for row in result.fetchall()}
        from src.server.repository import locations

        locations.fixes_gone(self._session, list(archived))  # out of sight, as trashed
        self._session.commit()
        return _split(asset_ids, archived)

    def archive_folder(self, library_id: str, folder: str | None) -> list[str]:
        """Archive every clip in sight under the folder (None: the whole library).
        The folder only picks the clips: a file added later shows up as usual."""
        result = self._session.execute(
            text(
                "UPDATE assets SET deleted_at = :now, deleted_reason = 'archived'"
                " WHERE library_id = :lib AND deleted_at IS NULL"
                "   AND (CAST(:under AS text) IS NULL OR rel_path LIKE :under ESCAPE '\\')"
                " RETURNING asset_id"
            ),
            {"now": utcnow(), "lib": library_id, "under": _under(folder)},
        )
        archived = [row[0] for row in result.fetchall()]
        from src.server.repository import locations

        locations.fixes_gone(self._session, archived)  # out of sight, as trashed
        self._session.commit()
        return archived

    def unarchive(self, asset_ids: list[str]) -> tuple[list[str], list[str]]:
        """Bring back clips a person archived. A missing file's clip comes back
        with its file, not with this; a trashed library's, with the library.
        Returns (unarchived, skipped)."""
        return self._bring_back(asset_ids, "a.deleted_reason = 'archived'" + self._IN_A_LIVE_LIBRARY)

    def unarchive_folder(self, library_id: str, folder: str | None) -> list[str]:
        """Unarchive every clip a person archived under the folder (None: the whole library)."""
        ids = list(self._session.execute(
            text(
                "SELECT asset_id FROM assets WHERE library_id = :lib AND deleted_reason = 'archived'"
                "   AND (CAST(:under AS text) IS NULL OR rel_path LIKE :under ESCAPE '\\')"
            ),
            {"lib": library_id, "under": _under(folder)},
        ).scalars().all())
        return self._bring_back(ids, "a.deleted_reason = 'archived'")[0]

    def bring_back_removed(self, asset_ids: list[str]) -> list[str]:
        """Clips a person trashed or archived, straight back into sight, the
        archive skipped too (a scan's --force-add: their files are on disk and
        a person asked for them). No commit: the caller's. Returns those back."""
        return self._bring_back(asset_ids, "a.deleted_reason IN ('user', 'archived')" + self._IN_A_LIVE_LIBRARY,
                                commit=False)[0]

    def _bring_back(self, asset_ids: list[str], condition: str, *, commit: bool = True) -> tuple[list[str], list[str]]:
        """Clips matching condition (SQL on alias a) back in sight and queued
        for search, with their scenes. Returns (back, skipped), in the order given."""
        if not asset_ids:
            return [], []
        sql = text(
            "UPDATE assets a SET deleted_at = NULL, deleted_reason = NULL, trashed_from = NULL,"
            " search_synced_at = NULL"
            f" WHERE a.asset_id = ANY(:ids) AND a.deleted_at IS NOT NULL AND {condition}"
            # Not onto a path another clip in sight holds now (a different file there is its own clip).
            "   AND NOT EXISTS (SELECT 1 FROM assets o WHERE o.library_id = a.library_id"
            "                   AND o.rel_path = a.rel_path AND o.deleted_at IS NULL AND o.asset_id <> a.asset_id)"
            " RETURNING a.asset_id"
        )
        from sqlalchemy.exc import IntegrityError

        try:
            with self._session.begin_nested():
                back = [row[0] for row in self._session.execute(sql, {"ids": asset_ids}).fetchall()]
        except IntegrityError:
            # Two of them at one path: one at a time, so the first holds it and the rest are skipped.
            back = []
            for asset_id in asset_ids:
                try:
                    with self._session.begin_nested():
                        back += [row[0] for row in self._session.execute(sql, {"ids": [asset_id]}).fetchall()]
                except IntegrityError:
                    pass
        if back:
            # Fixes back in sight: the guesses near them are checked again (ADR-017).
            from src.server.repository import locations

            locations.fixes_back(self._session, back)
        if commit:
            self._session.commit()
        return _split(asset_ids, set(back))

    def page_hidden(
        self,
        reasons: tuple[str, ...],
        *,
        library_id: str | None = None,
        folder: str | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
        within: list[str] | None = None,
    ) -> tuple[list[dict], int]:
        """Clips out of sight for these reasons, most recent first, outside
        trashed libraries (those come back or go with their library). Under a
        folder (recursive) if given; within: only these libraries. Returns
        (page, total matching)."""
        where = (
            " FROM assets a JOIN libraries l ON l.library_id = a.library_id"
            " WHERE a.deleted_at IS NOT NULL AND a.deleted_reason = ANY(:reasons) AND l.status <> 'trashed'"
            "   AND (CAST(:lib AS text) IS NULL OR a.library_id = :lib)"
            "   AND (CAST(:within AS text[]) IS NULL OR a.library_id = ANY(CAST(:within AS text[])))"
            "   AND (CAST(:under AS text) IS NULL OR a.rel_path LIKE :under ESCAPE '\\')"
        )
        params = {"reasons": list(reasons), "lib": library_id, "under": _under(folder), "within": within}
        total = int(self._session.execute(text("SELECT count(*)" + where), params).scalar() or 0)
        page_sql = where
        if after is not None:
            page_sql += " AND (a.deleted_at, a.asset_id) < (:ts, :aid)"
            params = {**params, "ts": after[0], "aid": after[1]}
        rows = self._session.execute(
            text(
                "SELECT a.asset_id, a.library_id, l.name AS library_name, a.rel_path, a.media_type,"
                " a.deleted_at, a.deleted_reason" + page_sql +
                " ORDER BY a.deleted_at DESC, a.asset_id DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows], total

    def library_ids_of(self, asset_ids: list[str]) -> list[str]:
        """The libraries these clips are in, whatever their state."""
        if not asset_ids:
            return []
        return list(self._session.execute(
            text("SELECT DISTINCT library_id FROM assets WHERE asset_id = ANY(:ids)"), {"ids": asset_ids}
        ).scalars().all())

    def find_at(self, library_id: str, rel_path: str, content_hash: str | None) -> Asset | None:
        """The clip archived or trashed at this path with this content: the old
        content back where it was. A person's trash or archive comes first (it
        stands); else the most recently missing."""
        if not content_hash:
            return None
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id, Asset.rel_path == rel_path, Asset.content_hash == content_hash,
                   Asset.deleted_at.is_not(None),  # type: ignore[union-attr]
                   Asset.deleted_reason.in_(("missing", "user", "archived")))  # type: ignore[union-attr]
            .order_by((Asset.deleted_reason == "missing").asc(), Asset.deleted_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        return self._session.exec(stmt).first()

    def missing_ids_with_hash(self, library_id: str, content_hash: str) -> list[str]:
        """The library's clips archived as missing with this content (no lock)."""
        return list(self._session.execute(
            text("SELECT asset_id FROM assets WHERE library_id = :lib AND content_hash = :hash"
                 "   AND deleted_at IS NOT NULL AND deleted_reason = 'missing'"
                 " ORDER BY deleted_at DESC, created_at DESC, asset_id"),
            {"lib": library_id, "hash": content_hash},
        ).scalars())

    def find_missing_by_hash(self, library_id: str, content_hash: str | None) -> Asset | None:
        """The library's most recently missing asset with this content,
        locked for restoring. A row someone else holds is waited for, not
        skipped: if another ingest claimed it meanwhile, it no longer matches
        and this file gets its own asset; if other work held it (a re-index, an
        OCR batch), it's still here to restore."""
        if not content_hash:
            return None
        stmt = (
            select(Asset)
            .where(Asset.library_id == library_id, Asset.content_hash == content_hash, _missing_file())
            .order_by(Asset.deleted_at.desc())
            .limit(1)
            .with_for_update()
        )
        return self._session.exec(stmt).first()

    def find_empty_newer_copy(self, asset: Asset) -> Asset | None:
        """For copy-then-delete: an active asset in the same library with the same
        content, made after this one, that no person has worked on. The first
        made, locked with its faces until the transaction ends.

        Lock first, then check: a rating, a confirmed person or a note that
        commits while we wait for the lock is seen by the check, and one that
        comes after waits for us (its asset or face is then gone, so it fails
        rather than vanishing)."""
        for copy_id in self.newer_copy_candidates(asset):
            copy = self.lock_copy(copy_id)
            if copy is not None and copy.content_hash == asset.content_hash and not self.has_human_data(copy_id):
                return copy
        return None

    def newer_copy_candidates(self, asset: Asset) -> list[str]:
        """Active assets in the same library with the same content, made after this one, first made first."""
        if not asset.content_hash:
            return []
        rows = self._session.execute(
            text(
                "SELECT asset_id FROM assets"
                " WHERE library_id = :library_id AND content_hash = :content_hash AND deleted_at IS NULL"
                "   AND asset_id <> :asset_id AND created_at > :created_at"
                " ORDER BY created_at, asset_id"
            ),
            {"library_id": asset.library_id, "content_hash": asset.content_hash, "asset_id": asset.asset_id,
             "created_at": asset.created_at},
        )
        return [r[0] for r in rows]

    def lock_copy(self, asset_id: str) -> Asset | None:
        """Lock an active asset and its faces (a confirmation or rejection of a
        person writes against the face). None if it's no longer active."""
        row = self._session.execute(
            text("SELECT asset_id FROM assets WHERE asset_id = :a AND deleted_at IS NULL FOR UPDATE"),
            {"a": asset_id},
        ).first()
        if row is None:
            return None
        self._session.execute(text("SELECT face_id FROM faces WHERE asset_id = :a ORDER BY face_id FOR UPDATE"),
                              {"a": asset_id})
        copy = self._session.get(Asset, asset_id)
        if copy is not None:
            self._session.refresh(copy)
        return copy

    def has_human_data(self, asset_id: str) -> bool:
        """Whether a person has worked on this asset: a note, a transcript they
        wrote or pasted, a correction, a rating, a project, a confirmed or
        rejected person, a location they set, or anything else made by a
        person (their lineage, e.g. the machine's transcript removed for good)."""
        return bool(self._session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM assets WHERE asset_id = :a"
                "               AND (note IS NOT NULL OR transcript_source = 'manual'))"
                " OR EXISTS (SELECT 1 FROM asset_corrections WHERE asset_id = :a)"
                " OR EXISTS (SELECT 1 FROM asset_ratings WHERE asset_id = :a)"
                " OR EXISTS (SELECT 1 FROM project_assets WHERE asset_id = :a)"
                " OR EXISTS (SELECT 1 FROM faces f JOIN face_person_matches m ON m.face_id = f.face_id"
                "            WHERE f.asset_id = :a AND m.confirmed)"
                " OR EXISTS (SELECT 1 FROM faces f JOIN face_person_rejections x ON x.face_id = f.face_id"
                "            WHERE f.asset_id = :a)"
                " OR EXISTS (SELECT 1 FROM asset_location WHERE asset_id = :a AND source = 'person')"
                " OR EXISTS (SELECT 1 FROM artifact_lineage WHERE asset_id = :a AND producer = 'person')"
            ),
            {"a": asset_id},
        ).scalar())

    def lock_for_restore(self, asset: Asset, rel_path: str) -> bool:
        """Lock an archived asset found at rel_path before restoring it there.
        False when, meanwhile, another ingest restored it at another path (a
        copy of the file, matched by content): rel_path is then unknown."""
        try:
            self._session.refresh(asset, with_for_update=True)
        except InvalidRequestError:
            # Deleted for good meanwhile (the trash emptied): nothing to restore.
            self._session.expunge(asset)
            return False
        return asset.rel_path == rel_path

    def lock_still_deleted(
        self, asset_ids: list[str], *, reason: str | None = None, before: datetime | None = None,
    ) -> list[str]:
        """Of these, the assets still deleted (for this reason, since before
        `before`), locked until the transaction ends: one restored, archived
        or trashed again since they were listed is left out."""
        if not asset_ids:
            return []
        rows = self._session.execute(
            text("SELECT asset_id FROM assets WHERE asset_id = ANY(:asset_ids) AND deleted_at IS NOT NULL"
                 "   AND (CAST(:reason AS text) IS NULL OR deleted_reason = :reason)"
                 "   AND (CAST(:before AS timestamptz) IS NULL OR deleted_at < :before)"
                 " ORDER BY asset_id FOR UPDATE"),
            {"asset_ids": asset_ids, "reason": reason, "before": before},
        )
        return [r[0] for r in rows]

    def count_expiring(self, cutoff: datetime) -> dict[str, int]:
        """What has been in the trash since before cutoff: clips a person
        trashed (outside trashed libraries), libraries, projects."""
        row = self._session.execute(text(
            "SELECT"
            " (SELECT count(*) FROM assets a JOIN libraries l ON l.library_id = a.library_id"
            "   WHERE a.deleted_reason = 'user' AND a.deleted_at < :c AND l.status <> 'trashed'),"
            " (SELECT count(*) FROM libraries WHERE status = 'trashed' AND trashed_at < :c),"
            " (SELECT count(*) FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < :c)"
        ), {"c": cutoff}).one()
        return {"clips": int(row[0]), "libraries": int(row[1]), "projects": int(row[2])}

    def clear_trash(self, asset: Asset) -> None:
        """Take an asset out of the trash: search finds it again (its words
        were kept), and its row is written again by the next search sync. No
        commit."""
        asset.deleted_at = None
        asset.deleted_reason = None
        asset.trashed_from = None
        asset.search_synced_at = None
        self._session.add(asset)
        # A fix back in sight: the guesses near it are checked again (ADR-017).
        from src.server.repository import locations

        locations.fixes_back(self._session, [asset.asset_id])

    VIDEO_FACET_FIELDS = (
        "duration_sec", "container", "video_codec", "width", "height", "rotation",
        "frame_rate_num", "frame_rate_den", "start_timecode", "drop_frame",
        "audio_codec", "audio_channels", "audio_sample_rate",
    )

    def upsert_video_facet(self, asset_id: str, facet: dict) -> None:
        """Store a video's probe result, replacing any earlier one.

        The probe's duration becomes the asset's duration_sec: it's measured
        from the stream, where the EXIF value may be missing (the macOS
        scanner sends none) or rounded.
        """
        values = {k: facet.get(k) for k in self.VIDEO_FACET_FIELDS}
        values["rotation"] = values["rotation"] or 0
        stmt = pg_insert(VideoFacetRow).values(asset_id=asset_id, probed_at=utcnow(), **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["asset_id"],
            set_={**values, "probed_at": stmt.excluded.probed_at},
        )
        self._session.execute(stmt)
        if values["duration_sec"] is not None:
            self._session.execute(
                text("UPDATE assets SET duration_sec = :d, updated_at = :now WHERE asset_id = :aid"),
                {"d": values["duration_sec"], "now": utcnow(), "aid": asset_id},
            )
        self._session.commit()

    def get_video_facet(self, asset_id: str) -> dict | None:
        row = self._session.get(VideoFacetRow, asset_id)
        if row is None:
            return None
        return {k: getattr(row, k) for k in self.VIDEO_FACET_FIELDS}

    def is_ignored(self, library_id: str, rel_path: str, content_hash: str | None = None) -> bool:
        """True if the user emptied this file's trash (see ignored_files). A
        different file at that path (both contents known) isn't: it's a new
        file (Robert, Oct 9)."""
        return self._session.execute(
            text("SELECT 1 FROM ignored_files WHERE library_id = :lib AND rel_path = :path"
                 "   AND (content_hash IS NULL OR CAST(:hash AS text) IS NULL OR content_hash = :hash) LIMIT 1"),
            {"lib": library_id, "path": normalize_rel_path(rel_path), "hash": content_hash},
        ).first() is not None

    def list_trashed(
        self,
        asset_ids: list[str] | None = None,
        trashed_before: datetime | None = None,
        limit: int | None = None,
        library_id: str | None = None,
        folder: str | None = None,
    ) -> list[Asset]:
        """Clips a person trashed, matching the filters, longest in the trash
        first: what the trash view shows. Archived clips (missing or archived
        by hand) are never here: they're deleted only by trashing them first.
        Nor are the clips of a library in the trash: they go with it."""
        stmt = (
            select(Asset)
            .join(Library, Library.library_id == Asset.library_id)  # type: ignore[arg-type]
            .where(Asset.deleted_at.isnot(None), Asset.deleted_reason == "user", Library.status != "trashed")
        )
        if asset_ids is not None:
            stmt = stmt.where(Asset.asset_id.in_(asset_ids))
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        if _under(folder) is not None:
            stmt = stmt.where(Asset.rel_path.like(_under(folder), escape="\\"))  # type: ignore[union-attr]
        if trashed_before is not None:
            stmt = stmt.where(Asset.deleted_at < trashed_before)
        stmt = stmt.order_by(Asset.deleted_at, Asset.asset_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.exec(stmt).all())

    def list_missing(self, library_id: str | None = None, folder: str | None = None,
                     limit: int | None = None, missing_before: datetime | None = None) -> list[Asset]:
        """Clips archived because their file went missing, matching the
        filters, longest missing first: what an admin can delete for good
        (Robert, Oct 9). Not those of a library in the trash: they go with it."""
        stmt = (
            select(Asset)
            .join(Library, Library.library_id == Asset.library_id)  # type: ignore[arg-type]
            .where(Asset.deleted_at.isnot(None), Asset.deleted_reason == "missing", Library.status != "trashed")
        )
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        if _under(folder) is not None:
            stmt = stmt.where(Asset.rel_path.like(_under(folder), escape="\\"))  # type: ignore[union-attr]
        if missing_before is not None:
            stmt = stmt.where(Asset.deleted_at < missing_before)
        stmt = stmt.order_by(Asset.deleted_at, Asset.asset_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.exec(stmt).all())

    def permanently_delete(self, asset_ids: list[str], *, commit: bool = True) -> int:
        """
        Hard delete trashed assets and all related rows in FK-safe order.
        Only deletes rows where deleted_at IS NOT NULL. Returns count of deleted asset rows.
        commit=False: the caller commits (a handover to a copy, with its archive).

        Every table that holds an ``asset_id`` foreign key must be cleaned
        here — none of the FK constraints are declared with
        ``ON DELETE CASCADE``, so missing one leaves orphans whose dead
        references break downstream lookups (the original symptom: cluster
        review surfaced face crops whose owning asset 404'd in the
        lightbox). The full child set is:

        - ``asset_metadata``        (1:1)
        - ``asset_embeddings``      (1:N per model)
        - ``video_scenes``          (video assets only)
        - ``video_index_chunks``    (video assets only)
        - ``faces`` + transitive ``face_person_matches`` and a NULL on
          ``people.representative_face_id`` so the FK on Person doesn't
          block the face delete
        - ``project_assets``     (membership rows)
        - ``asset_ratings``         (per-user)
        - ``video_facets``, ``ignored_files``' sources: ``video_facets``
          rows go with the asset via ON DELETE CASCADE
        - ``projects.cover_asset_id`` is nullable — set to NULL rather
          than deleting the project itself
        """
        # Only what's still deleted: a scan may have restored one since it was
        # listed, and its ratings, projects and faces stay.
        asset_ids = self.lock_still_deleted(asset_ids)
        if not asset_ids:
            return 0
        params = {"asset_ids": asset_ids}
        # Lumiverb never deletes originals, so a file the user trashed may
        # still be on disk. Remember it, or the next scan would bring it back;
        # a Live Photo's motion too, gone with its photo (live_photos.left_by).
        self._session.execute(
            text(
                "INSERT INTO ignored_files (library_id, rel_path, content_hash, file_size, file_mtime, created_at)"
                " SELECT a.library_id, a.rel_path, a.content_hash, a.file_size, a.file_mtime, :now FROM assets a"
                " WHERE a.asset_id = ANY(:asset_ids)"
                "   AND a.deleted_at IS NOT NULL AND a.deleted_reason IN ('user', 'live_photo')"
                # A file whose content isn't known names the whole path: not
                # one a clip in sight holds now (a changed file is a new clip).
                "   AND (a.content_hash IS NOT NULL OR NOT EXISTS (SELECT 1 FROM assets o"
                "        WHERE o.library_id = a.library_id AND o.rel_path = a.rel_path AND o.deleted_at IS NULL))"
                " ON CONFLICT (library_id, rel_path, (COALESCE(content_hash, ''))) DO NOTHING"
            ),
            {**params, "now": utcnow()},
        )
        self._session.execute(
            text("DELETE FROM asset_metadata WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM asset_embeddings WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM video_scenes WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM video_index_chunks WHERE asset_id = ANY(:asset_ids)"),
            params,
        )

        # Faces have two FKs pointing back at them — face_person_matches
        # and people.representative_face_id — so they must be cleared
        # before the face rows themselves can be deleted.
        self._session.execute(
            text(
                "DELETE FROM face_person_matches"
                " WHERE face_id IN ("
                "   SELECT face_id FROM faces WHERE asset_id = ANY(:asset_ids)"
                " )"
            ),
            params,
        )
        self._session.execute(
            text(
                "UPDATE people SET representative_face_id = NULL"
                " WHERE representative_face_id IN ("
                "   SELECT face_id FROM faces WHERE asset_id = ANY(:asset_ids)"
                " )"
            ),
            params,
        )
        self._session.execute(
            text("DELETE FROM faces WHERE asset_id = ANY(:asset_ids)"),
            params,
        )

        # Project membership and ratings reference asset_id directly.
        self._session.execute(
            text("DELETE FROM project_assets WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        self._session.execute(
            text("DELETE FROM asset_ratings WHERE asset_id = ANY(:asset_ids)"),
            params,
        )
        # Cover image is a nullable FK — null it out instead of cascading
        # the whole project.
        self._session.execute(
            text(
                "UPDATE projects SET cover_asset_id = NULL"
                " WHERE cover_asset_id = ANY(:asset_ids)"
            ),
            params,
        )

        # Same for library covers.
        self._session.execute(
            text(
                "UPDATE libraries SET cover_asset_id = NULL"
                " WHERE cover_asset_id = ANY(:asset_ids)"
            ),
            params,
        )

        result = self._session.execute(
            text(
                "DELETE FROM assets WHERE asset_id = ANY(:asset_ids) AND deleted_at IS NOT NULL RETURNING asset_id"
            ),
            params,
        )
        deleted_count = len(result.fetchall())
        if commit:
            self._session.commit()
        return deleted_count

    def update_proxy(
        self,
        asset_id: str,
        proxy_key: str,
        thumbnail_key: str,
        width: int,
        height: int,
        proxy_sha256: str | None = None,
        thumbnail_sha256: str | None = None,
    ) -> Asset:
        """
        Update asset proxy_key, thumbnail_key, width, height, status='proxy_ready', updated_at.

        If proxy_sha256/thumbnail_sha256 are provided (non-None), persist them too.
        """
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = proxy_key
        asset.thumbnail_key = thumbnail_key
        if proxy_sha256 is not None:
            asset.proxy_sha256 = proxy_sha256
        if thumbnail_sha256 is not None:
            asset.thumbnail_sha256 = thumbnail_sha256
        asset.width = width
        asset.height = height
        asset.status = asset_status.PROXY_READY
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()
        self._session.refresh(asset)
        return asset

    def set_status(self, asset_id: str, status: str) -> None:
        """Set asset.status to the given value and bump updated_at."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.status = status
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_video_preview(
        self,
        asset_id: str,
        video_preview_key: str,
    ) -> None:
        """Record video preview key and generated_at timestamp."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.video_preview_key = video_preview_key
        asset.video_preview_generated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_analysis_proxy(self, asset_id: str, key: str, sha256: str) -> None:
        """Record a video's analysis proxy."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.analysis_proxy_key = key
        asset.analysis_proxy_sha256 = sha256
        asset.analysis_proxy_generated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_proxy_artifact(
        self,
        asset_id: str,
        key: str,
        sha256: str,
        width: int | None,
        height: int | None,
    ) -> None:
        """Set proxy_key and proxy_sha256. Width/height updated only if non-None.

        Does NOT touch thumbnail_key or advance asset.status — status transitions
        are the job-complete path's responsibility, not the upload endpoint's.
        """
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.proxy_key = key
        asset.proxy_sha256 = sha256
        if width is not None:
            asset.width = width
        if height is not None:
            asset.height = height
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_thumbnail_artifact(self, asset_id: str, key: str, sha256: str) -> None:
        """Set thumbnail_key and thumbnail_sha256. Does NOT touch proxy_key or status."""
        asset = self._session.get(Asset, asset_id)
        if asset is None:
            raise ValueError(f"Asset not found: {asset_id}")
        asset.thumbnail_key = key
        asset.thumbnail_sha256 = sha256
        asset.updated_at = utcnow()
        self._session.add(asset)
        self._session.commit()

    def set_video_indexed(self, asset_id: str) -> None:
        """The clip's scenes are found (its last chunk is in); not committed:
        it lands with how they were found."""
        self._session.execute(text("UPDATE assets SET video_indexed = true, updated_at = now() WHERE asset_id = :a"),
                              {"a": asset_id})

    def update_exif(
        self,
        asset_id: str,
        content_hash: str | None,
        exif: dict,
        camera_make: str | None,
        camera_model: str | None,
        taken_at: str | None,
        gps_lat: float | None,
        gps_lon: float | None,
        duration_sec: float | None = None,
        iso: int | None = None,
        exposure_time_us: int | None = None,
        aperture: float | None = None,
        focal_length: float | None = None,
        focal_length_35mm: float | None = None,
        lens_model: str | None = None,
        flash_fired: bool | None = None,
        orientation: int | None = None,
        gps_accuracy_m: float | None = None,
        taken_at_offset_min: int | None = None,
        live_photo_id: str | None = None,
        live_photo_read: bool = False,
    ) -> None:
        """Update EXIF fields on asset record: what the file says. GPS that
        isn't a real position ((0, 0): no fix) is stored as none. The Live
        Photo identifier only when the scanner read it (live_photo_read):
        one that doesn't leaves what's kept."""
        from src.shared.location import is_fix

        taken_at_dt: datetime | None = None
        if taken_at:
            try:
                taken_at_dt = datetime.fromisoformat(taken_at)
            except ValueError:
                pass
        if not is_fix(gps_lat, gps_lon):
            gps_lat = gps_lon = gps_accuracy_m = None
        if not isinstance(gps_accuracy_m, (int, float)) or isinstance(gps_accuracy_m, bool) \
                or not 0 <= gps_accuracy_m < 1e7:
            gps_accuracy_m = None
        if not isinstance(taken_at_offset_min, int) or isinstance(taken_at_offset_min, bool) \
                or abs(taken_at_offset_min) > 14 * 60:
            taken_at_offset_min = None
        located = "taken_at, taken_at_offset_min, gps_lat, gps_lon"
        before = self._session.execute(
            text(f"SELECT {located} FROM assets WHERE asset_id = :a"), {"a": asset_id}).mappings().first()
        after = self._session.execute(
            text(
                f"""
                UPDATE assets SET
                    content_hash = :content_hash,
                    exif = :exif,
                    exif_extracted_at = :now,
                    camera_make = :camera_make,
                    camera_model = :camera_model,
                    taken_at = :taken_at,
                    gps_lat = :gps_lat,
                    gps_lon = :gps_lon,
                    gps_accuracy_m = :gps_accuracy_m,
                    taken_at_offset_min = :taken_at_offset_min,
                    duration_sec = COALESCE(:duration_sec, duration_sec),
                    iso = :iso,
                    exposure_time_us = :exposure_time_us,
                    aperture = :aperture,
                    focal_length = :focal_length,
                    focal_length_35mm = :focal_length_35mm,
                    lens_model = :lens_model,
                    flash_fired = :flash_fired,
                    orientation = :orientation,
                    live_photo_id = CASE WHEN :live_photo_read THEN :live_photo_id ELSE live_photo_id END
                WHERE asset_id = :asset_id
                RETURNING {located}
                """
            ),
            {
                "content_hash": content_hash,
                "exif": json.dumps(exif) if exif else None,
                "now": utcnow(),
                "camera_make": camera_make,
                "camera_model": camera_model,
                "taken_at": taken_at_dt,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "gps_accuracy_m": gps_accuracy_m,
                "taken_at_offset_min": taken_at_offset_min,
                "duration_sec": duration_sec,
                "iso": iso,
                "exposure_time_us": exposure_time_us,
                "aperture": aperture,
                "focal_length": focal_length,
                "focal_length_35mm": focal_length_35mm,
                "lens_model": lens_model,
                "flash_fired": flash_fired,
                "orientation": orientation,
                "live_photo_read": live_photo_read,
                "live_photo_id": live_photo_id,
                "asset_id": asset_id,
            },
        ).mappings().first()
        if after is not None:
            # A fix that appeared, moved or went: the guesses from it and near it are checked again (ADR-017).
            from src.server.repository import locations

            locations.file_changed(self._session, asset_id, dict(before) if before else None, dict(after))
        self._session.commit()

    def set_capture(self, asset_id: str, taken_at: datetime | None, taken_at_offset_min: int | None,
                    gps_accuracy_m: float | None) -> tuple[dict, dict] | None:
        """The capture facts read again from a clip's file (the capture
        producer): its taken_at (kept when the file gives none now), the zone
        and the GPS accuracy (only with the file's GPS). Returns (before,
        after) as {taken_at, taken_at_offset_min, gps_lat, gps_lon}, or None
        for a clip not in sight. Doesn't commit."""
        cols = "taken_at, taken_at_offset_min, gps_lat, gps_lon"
        before = self._session.execute(text(
            f"SELECT {cols} FROM active_assets WHERE asset_id = :a"), {"a": asset_id}).mappings().first()
        if before is None:
            return None
        after = self._session.execute(text(
            "UPDATE assets SET taken_at = COALESCE(CAST(:taken_at AS timestamptz), taken_at),"
            "  taken_at_offset_min = CAST(:offset AS integer),"
            "  gps_accuracy_m = CASE WHEN gps_lat IS NOT NULL AND gps_lon IS NOT NULL"
            "    THEN CAST(:accuracy AS double precision) END"
            f" WHERE asset_id = :a RETURNING {cols}"
        ), {"a": asset_id, "taken_at": taken_at, "offset": taken_at_offset_min,
            "accuracy": gps_accuracy_m}).mappings().one()
        return dict(before), dict(after)

    def get_by_ids(self, asset_ids: list[str]) -> list[Asset]:
        """Return active (non-trashed) assets for a list of asset_ids. Order not guaranteed."""
        if not asset_ids:
            return []
        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        return list(self._session.exec(stmt).all())


def _unsynced(session: Session, asset_id: str) -> None:
    """Before a machine's description or reading of the clip is saved, in
    its transaction: the clip's row is locked (a person's save waits for
    this one, and this for theirs: no copy lands under it), and stale for
    search until it's indexed. The search sweep marks a clip synced only if
    its row is as the sweep read it (search/sync.py), so this save, read or
    not, is never marked synced unseen: its time is taken before it commits,
    and a sweep that began in between would count it as older than itself."""
    session.execute(sa_text("UPDATE assets SET search_synced_at = NULL WHERE asset_id = :a"), {"a": asset_id})


class AssetOcrRepository:
    """The text read in each clip's image (one row per clip)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, asset_id: str, text: str, model_id: str = "", *, commit: bool = True) -> None:
        """The machine's reading. Text a person wrote for the clip stays the
        one copy: the machine's isn't kept under it (Robert, Oct 9)."""
        from src.server.repository.corrections import persons_fields

        _unsynced(self._session, asset_id)
        if "ocr_text" in persons_fields(self._session, asset_id):
            text = ""
        self._session.execute(sa_text(
            "INSERT INTO asset_ocr (asset_id, text, has_text, model_id, generated_at)"
            " VALUES (:a, :t, :h, :m, :now)"
            " ON CONFLICT (asset_id) DO UPDATE SET text = EXCLUDED.text, has_text = EXCLUDED.has_text,"
            "   model_id = EXCLUDED.model_id, generated_at = EXCLUDED.generated_at"
        ), {"a": asset_id, "t": text, "h": bool(text), "m": model_id, "now": utcnow()})
        if commit:
            self._session.commit()

    def text_for(self, asset_id: str) -> str:
        row = self._session.execute(sa_text("SELECT text FROM asset_ocr WHERE asset_id = :a"),
                                    {"a": asset_id}).first()
        return row[0] if row else ""


class AssetMetadataRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
        data: dict,
    ) -> None:
        """
        Insert or update metadata row for (asset_id, model_id, model_version).
        On conflict: update data and generated_at only. A description or tags
        a person wrote for the clip stay the one copy: the machine's aren't
        kept under them (Robert, Oct 9).
        """
        from src.server.repository.corrections import without_persons

        _unsynced(self._session, asset_id)
        data = without_persons(self._session, asset_id, data)
        now = utcnow()
        stmt = pg_insert(AssetMetadata).values(
            metadata_id="meta_" + str(ULID()),
            asset_id=asset_id,
            model_id=model_id,
            model_version=model_version,
            generated_at=now,
            data=data,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_metadata_asset_model_version",
            set_={
                "data": stmt.excluded.data,
                "generated_at": stmt.excluded.generated_at,
            },
        )
        self._session.execute(stmt)
        self._session.commit()

    def get(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
    ) -> AssetMetadata | None:
        stmt = select(AssetMetadata).where(
            AssetMetadata.asset_id == asset_id,
            AssetMetadata.model_id == model_id,
            AssetMetadata.model_version == model_version,
        )
        return self._session.exec(stmt).first()

    def get_latest(self, asset_id: str) -> AssetMetadata | None:
        """Return the most recent metadata row for the asset, regardless of model."""
        stmt = (
            select(AssetMetadata)
            .where(AssetMetadata.asset_id == asset_id)
            .order_by(AssetMetadata.generated_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        return self._session.exec(stmt).first()

    def list_for_asset(self, asset_id: str) -> list[AssetMetadata]:
        stmt = select(AssetMetadata).where(AssetMetadata.asset_id == asset_id)
        return list(self._session.exec(stmt).all())


class AssetEmbeddingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
        vector: list[float],
    ) -> None:
        """
        Insert or update the embedding for (asset_id, model_id, model_version).
        Uses ON CONFLICT DO UPDATE on the unique constraint.
        """
        stmt = pg_insert(AssetEmbedding).values(
            embedding_id="emb_" + str(ULID()),
            asset_id=asset_id,
            model_id=model_id,
            model_version=model_version,
            embedding_vector=vector,
            created_at=utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_asset_embeddings_asset_model_version",
            set_={"embedding_vector": vector, "created_at": utcnow()},
        )
        self._session.execute(stmt)
        self._session.commit()

    def get(
        self,
        asset_id: str,
        model_id: str,
        model_version: str,
    ) -> AssetEmbedding | None:
        stmt = select(AssetEmbedding).where(
            AssetEmbedding.asset_id == asset_id,
            AssetEmbedding.model_id == model_id,
            AssetEmbedding.model_version == model_version,
        )
        return self._session.exec(stmt).first()

    def get_any(self, asset_id: str) -> AssetEmbedding | None:
        """Return any embedding for the asset (most recent first).

        Used when the caller doesn't know which model was used — e.g. the
        web UI similarity search which should auto-detect the model.
        """
        stmt = (
            select(AssetEmbedding)
            .where(AssetEmbedding.asset_id == asset_id)
            .order_by(AssetEmbedding.created_at.desc())  # type: ignore[union-attr]
        )
        return self._session.exec(stmt).first()

    def find_similar(
        self,
        library_id: str,
        model_id: str,
        model_version: str,
        vector: list[float],
        limit: int,
        offset: int = 0,
        exclude_asset_id: str | None = None,
        scope: SimilarityScope | None = None,
    ) -> list[tuple[str, float]]:
        """
        Return (asset_id, distance) pairs ordered by cosine distance ASC.
        Filters to assets in library_id that are online.
        Optional scope applies extra filters (e.g. date range via taken_at).
        """
        conditions = [
            "a.library_id = :library_id",
            "a.availability = 'online'",
            "ae.model_id = :model_id",
            "ae.model_version = :model_version",
        ]
        params: dict = {
            "vec": str(vector),
            "library_id": library_id,
            "model_id": model_id,
            "model_version": model_version,
            "limit": limit,
            "offset": offset,
        }
        if exclude_asset_id is not None:
            conditions.append("ae.asset_id != :exclude_id")
            params["exclude_id"] = exclude_asset_id
        if scope and scope.date_range:
            dr = scope.date_range
            if dr.from_ts is not None:
                conditions.append(
                    "a.taken_at IS NOT NULL AND a.taken_at >= to_timestamp(:from_ts) AT TIME ZONE 'UTC'"
                )
                params["from_ts"] = dr.from_ts
            if dr.to_ts is not None:
                conditions.append(
                    "a.taken_at IS NOT NULL AND a.taken_at <= to_timestamp(:to_ts) AT TIME ZONE 'UTC'"
                )
                params["to_ts"] = dr.to_ts
        if scope and scope.asset_types and scope.asset_types != "all":
            # scope.asset_types is list["image" | "video"]; match media_type by prefix
            type_patterns = [f"{t}%" for t in scope.asset_types]
            placeholders = [f"a.media_type LIKE :asset_type_pat_{i}" for i in range(len(type_patterns))]
            conditions.append("(" + " OR ".join(placeholders) + ")")
            for i, pat in enumerate(type_patterns):
                params[f"asset_type_pat_{i}"] = pat
        if scope and scope.cameras:
            # OR across (make, model) pairs; within each pair AND
            cam_clauses = []
            for i, c in enumerate(scope.cameras):
                if c.make is not None and c.model is not None:
                    cam_clauses.append(
                        "(a.camera_make = :cam_make_{i} AND a.camera_model = :cam_model_{i})".format(i=i)
                    )
                    params[f"cam_make_{i}"] = c.make
                    params[f"cam_model_{i}"] = c.model
                elif c.make is not None:
                    cam_clauses.append("a.camera_make = :cam_make_{i}".format(i=i))
                    params[f"cam_make_{i}"] = c.make
                elif c.model is not None:
                    cam_clauses.append("a.camera_model = :cam_model_{i}".format(i=i))
                    params[f"cam_model_{i}"] = c.model
            if cam_clauses:
                conditions.append("(" + " OR ".join(cam_clauses) + ")")
        where = " AND ".join(conditions)
        sql = f"""
                SELECT ae.asset_id,
                       ae.embedding_vector <=> CAST(:vec AS vector) AS distance
                FROM asset_embeddings ae
                JOIN active_assets a ON a.asset_id = ae.asset_id
                WHERE {where}
                ORDER BY distance ASC
                LIMIT :limit OFFSET :offset
            """
        rows = self._session.execute(text(sql), params).fetchall()
        return [(r.asset_id, float(r.distance)) for r in rows]
