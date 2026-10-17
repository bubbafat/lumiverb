"""Saved views. Every repository takes a Session."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.tenant import (
    SavedView,
)
from ulid import ULID
from src.server.repository.projects import _SENTINEL


class SavedViewRepository:
    """CRUD for saved views (bookmarked filter presets)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, owner_user_id: str, name: str, query_params: str, icon: str | None = None) -> SavedView:
        max_pos = self._session.execute(
            text("SELECT COALESCE(MAX(position), -1) FROM saved_views WHERE owner_user_id = :uid"),
            {"uid": owner_user_id},
        ).scalar()
        view = SavedView(
            view_id=f"sv_{ULID()}",
            name=name,
            query_params=query_params,
            icon=icon,
            owner_user_id=owner_user_id,
            position=(max_pos or 0) + 1,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self._session.add(view)
        self._session.commit()
        self._session.refresh(view)
        return view

    def list_for_user(self, owner_user_id: str) -> list[SavedView]:
        stmt = (
            select(SavedView)
            .where(SavedView.owner_user_id == owner_user_id)
            .order_by(SavedView.position)
        )
        return list(self._session.exec(stmt).all())

    def get(self, view_id: str) -> SavedView | None:
        return self._session.get(SavedView, view_id)

    def update(self, view: SavedView, name: str | None = None, query_params: str | None = None, icon: object = _SENTINEL) -> SavedView:
        if name is not None:
            view.name = name
        if query_params is not None:
            view.query_params = query_params
        if icon is not _SENTINEL:
            view.icon = icon  # type: ignore[assignment]
        view.updated_at = utcnow()
        self._session.add(view)
        self._session.commit()
        self._session.refresh(view)
        return view

    def delete(self, view: SavedView) -> None:
        self._session.delete(view)
        self._session.commit()

    def reorder(self, owner_user_id: str, view_ids: list[str]) -> None:
        for i, vid in enumerate(view_ids):
            self._session.execute(
                text("UPDATE saved_views SET position = :pos, updated_at = :now WHERE view_id = :vid AND owner_user_id = :uid"),
                {"pos": i, "now": utcnow(), "vid": vid, "uid": owner_user_id},
            )
        self._session.commit()

    def delete_for_user(self, user_id: str) -> int:
        from sqlalchemy import delete as sa_delete
        result = self._session.execute(
            sa_delete(SavedView).where(SavedView.owner_user_id == user_id)
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]
