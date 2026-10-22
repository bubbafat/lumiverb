"""Ratings: a person's stars, flags and colours on clips. Every repository takes a Session."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session, select

from src.server import cursors
from src.shared.utils import utcnow
from src.server.models.tenant import (
    Asset,
    AssetRating,
)
from src.server.repository.projects import _SENTINEL


# ---------------------------------------------------------------------------
# Ratings (ADR-007)
# ---------------------------------------------------------------------------


class RatingRepository:
    """Repository for user-scoped asset ratings (favorites, stars, color labels)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(
        self,
        user_id: str,
        asset_id: str,
        *,
        favorite: bool | None = None,
        stars: int | None = None,
        color: str | object = _SENTINEL,
    ) -> AssetRating | None:
        """Set or update a rating. Only provided fields are changed.

        Returns the rating, or None if the row was deleted (all-default state).
        """
        existing = self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id == asset_id,
            )
        ).first()

        if existing:
            if favorite is not None:
                existing.favorite = favorite
            if stars is not None:
                existing.stars = stars
            if color is not _SENTINEL:
                existing.color = color  # type: ignore[assignment]
            existing.updated_at = utcnow()

            # Delete row if all-default
            if not existing.favorite and existing.stars == 0 and existing.color is None:
                self._session.delete(existing)
                self._session.commit()
                return None

            self._session.add(existing)
            self._session.commit()
            self._session.refresh(existing)
            return existing

        # New row — apply defaults for unprovided fields
        fav = favorite if favorite is not None else False
        st = stars if stars is not None else 0
        col = color if color is not _SENTINEL else None

        # Don't insert if all-default
        if not fav and st == 0 and col is None:
            return None

        rating = AssetRating(
            user_id=user_id,
            asset_id=asset_id,
            favorite=fav,
            stars=st,
            color=col,  # type: ignore[arg-type]
            updated_at=utcnow(),
        )
        self._session.add(rating)
        self._session.commit()
        self._session.refresh(rating)
        return rating

    def get_for_asset(self, user_id: str, asset_id: str) -> AssetRating | None:
        return self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id == asset_id,
            )
        ).first()

    def get_for_assets(
        self, user_id: str, asset_ids: list[str]
    ) -> dict[str, AssetRating]:
        """Bulk read — returns dict keyed by asset_id."""
        if not asset_ids:
            return {}
        rows = self._session.exec(
            select(AssetRating).where(
                AssetRating.user_id == user_id,
                AssetRating.asset_id.in_(asset_ids),  # type: ignore[attr-defined]
            )
        ).all()
        return {r.asset_id: r for r in rows}

    def batch_upsert(
        self,
        user_id: str,
        asset_ids: list[str],
        *,
        favorite: bool | None = None,
        stars: int | None = None,
        color: str | object = _SENTINEL,
    ) -> int:
        """Apply the same rating update to multiple assets. Returns count updated."""
        if not asset_ids:
            return 0

        updated = 0
        for asset_id in asset_ids:
            self.upsert(
                user_id, asset_id, favorite=favorite, stars=stars, color=color
            )
            updated += 1
        return updated

    def list_favorites(
        self, user_id: str, after: str | None = None, limit: int = 200, within: list[str] | None = None,
    ) -> tuple[list[Asset], str | None]:
        """Return active favorited assets across all libraries (only these
        when within is given), newest first.

        Uses cursor pagination on asset_ratings.updated_at DESC.
        Returns (assets, next_cursor).
        """
        conditions = [
            "r.user_id = :user_id",
            "r.favorite = TRUE",
            "a.deleted_at IS NULL",
        ]
        params: dict = {"user_id": user_id, "limit": limit + 1}
        if within is not None:
            conditions.append("a.library_id = ANY(:within)")
            params["within"] = list(within)

        if after:
            conditions.append("r.updated_at < :after_ts")
            params["after_ts"] = cursors.time(after)  # the last one's time; anything else a 400

        where_sql = " AND ".join(conditions)
        sql = f"""
            SELECT a.asset_id, r.updated_at
            FROM asset_ratings r
            JOIN assets a ON a.asset_id = r.asset_id
            WHERE {where_sql}
            ORDER BY r.updated_at DESC
            LIMIT :limit
        """
        rows = self._session.execute(text(sql).bindparams(**params)).all()

        asset_ids = [row[0] for row in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit:
            next_cursor = rows[limit - 1][1].isoformat()

        if not asset_ids:
            return [], None

        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        assets_by_id = {a.asset_id: a for a in self._session.exec(stmt).all()}
        ordered = [assets_by_id[aid] for aid in asset_ids if aid in assets_by_id]
        return ordered, next_cursor

    def delete_for_user(self, user_id: str) -> int:
        """Delete all ratings for a user. Used when a user account is deleted."""
        from sqlalchemy import delete as sa_delete

        result = self._session.execute(
            sa_delete(AssetRating).where(AssetRating.user_id == user_id)
        )
        self._session.commit()
        return result.rowcount  # type: ignore[return-value]
