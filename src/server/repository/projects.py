"""Projects and the clips in them. Every repository takes a Session."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, false, func, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import text as sa_text
from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.tenant import (
    Asset,
    Project,
    ProjectAsset,
)
from ulid import ULID


# ---------------------------------------------------------------------------
# Projects (ADR-006)
# ---------------------------------------------------------------------------

_SENTINEL = object()  # distinguishes "not provided" from None


class ProjectRepository:
    """Repository for projects and project_assets tables.

    within: the libraries the caller can see (ADR-018 decision 8, roles per
    library; None: every library). A project's clips, counts, cover and
    order are of its clips in those; a project is listed when the caller
    made it or can see one of its clips."""

    def __init__(self, session: Session, within: list[str] | None = None) -> None:
        self._session = session
        self.within = None if within is None else list(within)

    def _seen(self, query):
        """The query (over Asset) kept to the libraries within."""
        return query if self.within is None else query.where(Asset.library_id.in_(self.within))  # type: ignore[attr-defined]

    def _seen_sql(self, params: dict, alias: str = "a") -> str:
        """SQL: the clip (alias) in the libraries within; params gets them."""
        params["within"] = self.within
        return f"(CAST(:within AS text[]) IS NULL OR {alias}.library_id = ANY(CAST(:within AS text[])))"

    def shows(self, project: Project, user_id: str) -> bool:
        """Whether the caller sees the project for its clips: they made it, or
        one of its clips is in a library they can see (every one does when
        within is None)."""
        if self.within is None or (project.owner_user_id is not None and project.owner_user_id == user_id):
            return True
        params = {"pid": project.project_id}
        return self._session.execute(text(
            "SELECT 1 FROM project_assets pa JOIN assets a ON a.asset_id = pa.asset_id"
            f" WHERE pa.project_id = :pid AND {self._seen_sql(params)} LIMIT 1"), params).first() is not None

    # ---- Project CRUD ----

    def create(
        self,
        name: str,
        owner_user_id: str | None = None,
        description: str | None = None,
        sort_order: str = "manual",
        visibility: str = "private",
    ) -> Project:
        project_id = "prj_" + str(ULID())  # pre-rename ids start with "col_"
        project = Project(
            project_id=project_id,
            name=name,
            owner_user_id=owner_user_id,
            description=description,
            sort_order=sort_order,
            visibility=visibility,
        )
        self._session.add(project)
        self._session.commit()
        self._session.refresh(project)
        return project

    def get_by_id(self, project_id: str, *, include_trashed: bool = False) -> Project | None:
        """A project by id. A trashed one counts as gone unless include_trashed."""
        stmt = select(Project).where(Project.project_id == project_id)
        if not include_trashed:
            stmt = stmt.where(Project.deleted_at.is_(None))  # type: ignore[union-attr]
        return self._session.exec(stmt).first()

    def list_for_user(self, user_id: str, *, statuses: tuple[str, ...] = ("active",)) -> list[Project]:
        """Return projects owned by user + shared projects, in the given
        lifecycle states: those the caller sees for their clips (shows)."""
        return [p for p in self._listed_for_user(user_id, statuses) if self.shows(p, user_id)]

    def _listed_for_user(self, user_id: str, statuses: tuple[str, ...]) -> list[Project]:
        return list(
            self._session.exec(
                select(Project)
                .where(
                    or_(
                        Project.owner_user_id == user_id,
                        Project.owner_user_id.is_(None),  # type: ignore[union-attr]
                        Project.visibility.in_(["shared", "public"]),  # type: ignore[union-attr]
                    ),
                    Project.status.in_(statuses),  # type: ignore[attr-defined]
                    Project.deleted_at.is_(None),  # type: ignore[union-attr]
                )
                .order_by(Project.created_at.desc())  # type: ignore[attr-defined]
            ).all()
        )

    def list_trashed_before(self, cutoff: datetime) -> list[Project]:
        """Every project trashed before cutoff, whoever owns it: the trash days are the account's."""
        stmt = select(Project).where(Project.deleted_at.is_not(None), Project.deleted_at < cutoff)  # type: ignore[union-attr]
        return list(self._session.exec(stmt).all())

    def list_trashed(self, user_id: str, *, nobodys: bool = False) -> list[Project]:
        """The user's trash: projects they own that were deleted, and with
        nobodys (an account admin's) those nobody owns, most recently deleted
        first."""
        return list(
            self._session.exec(
                select(Project)
                .where(
                    or_(
                        Project.owner_user_id == user_id,
                        Project.owner_user_id.is_(None) if nobodys else false(),  # type: ignore[union-attr]
                    ),
                    Project.deleted_at.is_not(None),  # type: ignore[union-attr]
                )
                .order_by(Project.deleted_at.desc())  # type: ignore[union-attr]
            ).all()
        )

    def trash(self, project_id: str) -> bool:
        """Move a project to the trash. False if it's missing or already there."""
        col = self.get_by_id(project_id)
        if col is None:
            return False
        col.deleted_at = col.updated_at = utcnow()
        self._session.add(col)
        self._session.commit()
        return True

    def lock_trashed(self, project_id: str, before: datetime | None = None) -> bool:
        """Lock the project until the transaction ends if it's still in the trash
        (since before `before`): what deleting it for good re-checks, and what
        restoring it waits for."""
        return self._session.execute(
            text("SELECT 1 FROM projects WHERE project_id = :p AND deleted_at IS NOT NULL"
                 "   AND (CAST(:before AS timestamptz) IS NULL OR deleted_at < :before) FOR UPDATE"),
            {"p": project_id, "before": before},
        ).first() is not None

    def restore(self, project_id: str) -> bool:
        """Take a project out of the trash. False if it isn't in the trash (or a
        purge holding it deleted it meanwhile)."""
        if not self.lock_trashed(project_id):
            self._session.rollback()
            return False
        col = self.get_by_id(project_id, include_trashed=True)
        if col is None or col.deleted_at is None:
            return False
        col.deleted_at = None
        col.updated_at = utcnow()
        self._session.add(col)
        self._session.commit()
        return True

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = _SENTINEL,
        visibility: str | None = None,
        sort_order: str | None = None,
        cover_asset_id: str | None = _SENTINEL,
        status: str | None = None,
    ) -> Project | None:
        col = self.get_by_id(project_id)
        if col is None:
            return None
        if status is not None and status != col.status:
            col.status = status
            col.archived_at = utcnow() if status == "archived" else None
        if name is not None:
            col.name = name
        if description is not _SENTINEL:
            col.description = description
        if visibility is not None:
            col.visibility = visibility
        if sort_order is not None:
            col.sort_order = sort_order
        if cover_asset_id is not _SENTINEL:
            col.cover_asset_id = cover_asset_id
        col.updated_at = utcnow()
        self._session.add(col)
        self._session.commit()
        self._session.refresh(col)
        return col

    def delete(self, project_id: str) -> bool:
        """Delete a project for good, trashed or not. Its clips stay."""
        col = self.get_by_id(project_id, include_trashed=True)
        if col is None:
            return False
        self._session.delete(col)
        self._session.commit()
        return True

    def has_asset(self, project_id: str, asset_id: str) -> bool:
        """Whether the clip is in the project (and in a library within)."""
        query = (select(ProjectAsset).join(Asset, ProjectAsset.asset_id == Asset.asset_id)
                 .where(ProjectAsset.project_id == project_id, ProjectAsset.asset_id == asset_id))
        return self._session.exec(self._seen(query)).first() is not None

    # ---- Asset count (no denormalized column) ----

    def asset_count(self, project_id: str) -> int:
        result = self._session.execute(self._seen(
            select(func.count())
            .select_from(ProjectAsset)
            .join(Asset, ProjectAsset.asset_id == Asset.asset_id)
            .where(
                ProjectAsset.project_id == project_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ))
        return int(result.scalar() or 0)

    # Clips still linked to a project but hidden, in kinds that need
    # different actions: a person trashed them (restore), a person archived
    # them (unarchive), a scan found the file missing (back when the file
    # is), or their library is in the trash (back with the library). A Live
    # Photo's motion is none of these: it's part of its photo, which shows.
    _HIDDEN_CLIPS_SQL = (
        "SELECT"
        " count(*) FILTER (WHERE l.status <> 'trashed' AND a.deleted_reason = 'user') AS trashed,"
        " count(*) FILTER (WHERE l.status <> 'trashed'"
        "   AND a.deleted_reason NOT IN ('user', 'archived', 'live_photo')) AS missing,"
        " count(*) FILTER (WHERE l.status = 'trashed') AS library_trashed,"
        " count(*) FILTER (WHERE l.status <> 'trashed' AND a.deleted_reason = 'archived') AS archived"
        " FROM project_assets pa"
        " JOIN assets a ON a.asset_id = pa.asset_id"
        " JOIN libraries l ON l.library_id = a.library_id"
        " WHERE pa.project_id = :pid AND a.deleted_at IS NOT NULL"
    )

    def hidden_clip_counts(self, project_id: str) -> dict[str, int]:
        """{"trashed", "missing", "library_trashed", "archived"} for the project's
        hidden clips, in one query."""
        params: dict = {"pid": project_id}
        row = self._session.execute(text(self._HIDDEN_CLIPS_SQL + f" AND {self._seen_sql(params)}"), params).one()
        return {"trashed": int(row[0]), "missing": int(row[1]), "library_trashed": int(row[2]),
                "archived": int(row[3])}

    def trashed_asset_ids(self, project_id: str, *, edited: list[str] | None = None) -> list[str]:
        """The clips in the project that a person trashed, whose library
        isn't in the trash: the ones restoring with the project brings back
        (of those in the libraries edited, when given: the caller's)."""
        return list(self._session.execute(
            text(
                "SELECT a.asset_id FROM project_assets pa"
                " JOIN assets a ON a.asset_id = pa.asset_id"
                " JOIN libraries l ON l.library_id = a.library_id"
                " WHERE pa.project_id = :pid AND a.deleted_at IS NOT NULL"
                "   AND a.deleted_reason = 'user' AND l.status <> 'trashed'"
                "   AND (CAST(:edited AS text[]) IS NULL OR a.library_id = ANY(CAST(:edited AS text[])))"
            ),
            {"pid": project_id, "edited": edited},
        ).scalars().all())

    def _usage_filter(self, asset_ids: list[str], library_ids: list[str]):
        """Membership rows for these clips, or for any clip in these libraries."""
        conds = []
        if asset_ids:
            conds.append(ProjectAsset.asset_id.in_(asset_ids))  # type: ignore[attr-defined]
        if library_ids:
            conds.append(ProjectAsset.asset_id.in_(  # type: ignore[attr-defined]
                select(Asset.asset_id).where(Asset.library_id.in_(library_ids))  # type: ignore[attr-defined]
            ))
        return or_(*conds) if conds else None

    def usage(
        self, asset_ids: list[str], library_ids: list[str] | None = None
    ) -> tuple[list[tuple[Project, int]], int]:
        """Every project (trashed or not) holding any of these clips, or any
        clip in these libraries, with how many it holds; and how many of the
        clips are in any project."""
        cond = self._usage_filter(asset_ids, library_ids or [])
        if cond is None:
            return [], 0
        rows = self._session.execute(
            select(Project, func.count(ProjectAsset.asset_id))
            .join(ProjectAsset, ProjectAsset.project_id == Project.project_id)
            .where(cond)
            .group_by(Project.project_id)
            .order_by(Project.name)
        ).all()
        in_any = self._session.execute(
            select(func.count(func.distinct(ProjectAsset.asset_id))).where(cond)
        ).scalar()
        return [(row[0], int(row[1])) for row in rows], int(in_any or 0)

    # ---- Batch add / remove ----

    def add_assets(self, project_id: str, asset_ids: list[str]) -> int:
        """Add assets to project. Returns count actually inserted (idempotent)."""
        if not asset_ids:
            return 0

        # Get current max position
        max_pos_result = self._session.execute(
            select(func.max(ProjectAsset.position)).where(
                ProjectAsset.project_id == project_id
            )
        )
        max_pos = max_pos_result.scalar()
        next_pos = 0 if max_pos is None else max_pos + 1  # 0 is a position too

        inserted = 0
        for asset_id in asset_ids:
            stmt = pg_insert(ProjectAsset).values(
                project_id=project_id,
                asset_id=asset_id,
                position=next_pos,
                added_at=utcnow(),
            ).on_conflict_do_nothing(index_elements=["project_id", "asset_id"])
            result = self._session.execute(stmt)
            if result.rowcount:  # type: ignore[union-attr]
                inserted += 1
                next_pos += 1
        self._session.commit()
        return inserted

    def remove_assets(self, project_id: str, asset_ids: list[str]) -> int:
        """Remove assets from project. Returns count removed."""
        if not asset_ids:
            return 0
        from sqlalchemy import delete as sa_delete

        result = self._session.execute(
            sa_delete(ProjectAsset).where(
                ProjectAsset.project_id == project_id,
                ProjectAsset.asset_id.in_(asset_ids),  # type: ignore[attr-defined]
            )
        )
        # A chosen cover that leaves the project is no longer a choice.
        self._session.execute(
            text(
                "UPDATE projects SET cover_asset_id = NULL"
                " WHERE project_id = :pid AND cover_asset_id = ANY(:ids)"
            ),
            {"pid": project_id, "ids": list(asset_ids)},
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]

    # ---- List assets (paginated, ordered) ----

    def list_assets(
        self,
        project_id: str,
        sort_order: str = "manual",
        after_cursor: str | None = None,
        limit: int = 200,
    ) -> tuple[list[Asset], str | None]:
        """Return active assets in project with cursor pagination.

        Returns (assets, next_cursor). Cursor is the position/added_at/taken_at value
        of the last returned row, encoded as a string.
        """
        import base64 as _b64

        if sort_order == "added_at":
            order_col = ProjectAsset.added_at
        elif sort_order == "taken_at":
            # Capture time, never NULL so the cursor can compare it: assets
            # without EXIF fall back to file mtime, then to when they were
            # added (the same rule as the date filter).
            order_col = func.coalesce(Asset.taken_at, Asset.file_mtime, Asset.created_at)
        else:  # manual
            order_col = ProjectAsset.position

        query = self._seen(
            select(Asset, order_col.label("sort_value"))
            .join(ProjectAsset, ProjectAsset.asset_id == Asset.asset_id)
            .where(
                ProjectAsset.project_id == project_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
        query = query.order_by(order_col.asc(), Asset.asset_id.asc())  # type: ignore[union-attr]

        if after_cursor:
            try:
                padded = after_cursor + "=" * (-len(after_cursor) % 4)
                decoded = json.loads(_b64.urlsafe_b64decode(padded))
                cursor_val = decoded["v"]
                cursor_id = decoded["id"]
                if sort_order in ("added_at", "taken_at"):
                    cursor_val = datetime.fromisoformat(cursor_val)
                query = query.where(
                    or_(
                        order_col > cursor_val,  # type: ignore[operator]
                        and_(order_col == cursor_val, Asset.asset_id > cursor_id),  # type: ignore[operator]
                    )
                )
            except Exception:
                pass  # ignore bad cursors

        rows = self._session.execute(query.limit(limit + 1)).all()

        assets: list[Asset] = []
        next_cursor: str | None = None
        for i, row in enumerate(rows):
            if i >= limit:
                # Encode cursor from the last returned row's sort value
                last_asset = assets[-1]
                sort_value = rows[i - 1][1]
                if hasattr(sort_value, "isoformat"):
                    sort_value = sort_value.isoformat()
                cursor_payload = json.dumps(
                    {"v": sort_value, "id": last_asset.asset_id},
                    default=str,
                )
                next_cursor = _b64.urlsafe_b64encode(cursor_payload.encode()).decode().rstrip("=")
                break
            assets.append(row[0])

        return assets, next_cursor

    # ---- Reorder ----

    def reorder(self, project_id: str, asset_ids: list[str]) -> bool:
        """Reorder assets in project. asset_ids must include ALL active assets
        (the caller sees: the libraries within). Those the caller can't see
        keep their places; the others take the places they had, in the new
        order.

        Returns True on success. Raises ValueError if list is incomplete/has extras.
        """
        # Get current active asset IDs in project, and the places they're in
        rows = self._session.execute(self._seen(
            select(ProjectAsset.asset_id, ProjectAsset.position)
            .join(Asset, ProjectAsset.asset_id == Asset.asset_id)
            .where(
                ProjectAsset.project_id == project_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )).all()
        current_ids = {r[0] for r in rows}
        submitted_ids = set(asset_ids)

        if current_ids != submitted_ids:
            raise ValueError(
                f"Submitted {len(submitted_ids)} asset IDs but project has {len(current_ids)} active assets. "
                "Reorder must include all active assets in the project."
            )

        places = sorted(r[1] for r in rows) if self.within is not None else range(len(asset_ids))
        for position, asset_id in zip(places, asset_ids, strict=True):
            self._session.execute(
                sa_text(
                    "UPDATE project_assets SET position = :pos "
                    "WHERE project_id = :cid AND asset_id = :aid"
                ),
                {"pos": position, "cid": project_id, "aid": asset_id},
            )
        self._session.commit()
        return True

    # ---- Cover resolution ----

    def resolve_cover(self, project: Project) -> str | None:
        """The cover to show: the chosen one if it's in the project and in
        sight, else the first active clip by position (Robert, Oct 9).

        Reading never clears the choice: a chosen cover whose file went
        missing is the cover again when it's back. The choice is cleared when
        a person trashes the clip (trash_many), it leaves the project
        (remove_assets), or it's deleted for good."""
        if project.cover_asset_id:
            row = self._session.execute(self._seen(
                select(ProjectAsset.asset_id)
                .join(Asset, ProjectAsset.asset_id == Asset.asset_id)
                .where(
                    ProjectAsset.project_id == project.project_id,
                    ProjectAsset.asset_id == project.cover_asset_id,
                    Asset.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            )).first()
            if row:
                return project.cover_asset_id

        # Fallback: first active asset by position (that the caller sees)
        row = self._session.execute(self._seen(
            select(ProjectAsset.asset_id)
            .join(Asset, ProjectAsset.asset_id == Asset.asset_id)
            .where(
                ProjectAsset.project_id == project.project_id,
                Asset.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).order_by(ProjectAsset.position.asc()).limit(1)).first()  # type: ignore[union-attr]
        return row[0] if row else None
