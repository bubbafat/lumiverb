"""Browsing a library's clips: the grid's queries. Every repository takes a Session."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlmodel import Session, select

from src.shared.utils import escape_like
from src.server.repository import corrections as _corr
from src.server.models.tenant import (
    Asset,
)


class UnifiedBrowseRepository:
    """Cross-library browse — queries active_assets without library_id constraint."""

    SORTABLE_COLUMNS = {
        "asset_id", "taken_at", "created_at", "file_size",
        "iso", "exposure_time_us", "aperture", "focal_length", "rel_path",
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    def _DELETED_OLD_page(
        self,
        *,
        filters: "object | None" = None,
        rating_user_id: str | None = None,
        after: str | None = None,
        limit: int = 500,
    ) -> list[Asset]:
        """Keyset pagination across all libraries using BrowseFilters."""
        from src.server.models.browse_filters import BrowseFilters as _BF
        import base64 as _b64
        import math as _math

        if filters is None:
            filters = _BF()

        sort_col = filters.sort if filters.sort in self.SORTABLE_COLUMNS else "taken_at"
        is_desc = filters.direction.lower() == "desc"
        cmp_op = "<" if is_desc else ">"
        order_dir = "DESC" if is_desc else "ASC"

        conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}

        # --- Library filter ---
        if filters.library_ids:
            conditions.append("a.library_id = ANY(:library_ids)")
            params["library_ids"] = filters.library_ids

        # --- Path prefix (requires library_ids) ---
        if filters.path_prefix:
            conditions.append(
                "(a.rel_path = :path_prefix OR a.rel_path LIKE :path_prefix_like ESCAPE '\\')"
            )
            params["path_prefix"] = filters.path_prefix
            params["path_prefix_like"] = escape_like(filters.path_prefix) + "/%"

        # --- Composite cursor ---
        if after is not None:
            cursor_value = None
            cursor_id = after
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_value = decoded["v"]
                cursor_id = decoded["id"]
            except Exception:
                if sort_col != "asset_id":
                    sort_col = "asset_id"

            if sort_col == "asset_id":
                conditions.append(f"a.asset_id {cmp_op} :cursor_id")
                params["cursor_id"] = cursor_id
            else:
                conditions.append(f"""(
                    -- Rows are ordered NULLS LAST: after an undated cursor only
                    -- undated rows follow; after a dated one, later dated rows
                    -- and every undated row.
                    CASE
                        WHEN :cursor_value IS NULL THEN
                            a.{sort_col} IS NULL AND a.asset_id {cmp_op} :cursor_id
                        WHEN a.{sort_col} IS NULL THEN
                            TRUE
                        ELSE
                            (a.{sort_col}, a.asset_id) {cmp_op} (:cursor_value, :cursor_id)
                    END
                )""")
                params["cursor_value"] = cursor_value
                params["cursor_id"] = cursor_id

        # Apply filters from BrowseFilters object
        join_ratings, join_metadata = _apply_browse_filters(
            filters, conditions, params, rating_user_id, _math
        )

        # --- Build query ---
        where_sql = " AND ".join(conditions) if conditions else "TRUE"

        lateral_join = ""
        if join_metadata:
            lateral_join = _corr.TAGS_JOIN  # the tags a person sees

        rating_join_sql = ""
        if join_ratings:
            rating_join_sql = """
            LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id AND r.user_id = :rating_user_id
            """

        if sort_col == "asset_id":
            order_clause = f"a.asset_id {order_dir}"
        else:
            order_clause = f"a.{sort_col} {order_dir} NULLS LAST, a.asset_id {order_dir}"

        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            {lateral_join}
            {rating_join_sql}
            WHERE {where_sql}
            ORDER BY {order_clause}
            LIMIT :limit
        """
        result = self._session.execute(text(id_sql).bindparams(**params))
        asset_ids = [row[0] for row in result.all()]
        if not asset_ids:
            return []
        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        assets_by_id = {a.asset_id: a for a in self._session.exec(stmt).all()}
        return [assets_by_id[aid] for aid in asset_ids if aid in assets_by_id]


    def query_page(
        self,
        *,
        spec: "QuerySpec",
        candidate_ids: list[str] | None = None,
        candidate_scores: dict[str, float] | None = None,
        rating_user_id: str | None = None,
        after: str | None = None,
        limit: int = 500,
    ) -> list[Asset]:
        """Keyset pagination using the QueryFilter algebra.

        If candidate_ids is provided (from the text search), an
        additional ``a.asset_id = ANY(:candidate_ids)`` filter is
        applied. When candidate_scores is also provided, the result is
        ordered by relevance (score DESC) instead of the default
        chronological sort — text search ALWAYS prefers relevance,
        because returning Cards/* mixed with chronologically-newer
        non-Cards results was the core "search is confusing" complaint.
        """
        import base64 as _b64

        # Text search forces relevance ordering. Without this override
        # the default sort=taken_at runs and the search's ranking is
        # thrown on the floor.
        relevance_order = candidate_ids is not None and candidate_scores is not None
        sort_col = spec.sort if spec.sort in self.SORTABLE_COLUMNS else "taken_at"
        is_desc = spec.direction.lower() == "desc"
        cmp_op = "<" if is_desc else ">"
        order_dir = "DESC" if is_desc else "ASC"

        conditions: list[str] = []
        params: dict[str, object] = {"limit": limit}
        counter = [0]

        # --- Candidate set from text search ---
        # When in relevance mode we sort the candidate id list by score
        # descending up here, and feed that ordered list to a SQL
        # `array_position` ORDER BY below. The dict-of-floats only
        # exists to do this sort — once it's done, only the order
        # matters.
        ordered_candidate_ids: list[str] | None = None
        if candidate_ids is not None:
            conditions.append("a.asset_id = ANY(:candidate_ids)")
            params["candidate_ids"] = candidate_ids
            if relevance_order:
                # mypy guard — the relevance_order branch implies non-None
                assert candidate_scores is not None
                ordered_candidate_ids = sorted(
                    candidate_ids,
                    key=lambda aid: (-candidate_scores.get(aid, 0.0), aid),
                )
                params["ordered_candidate_ids"] = ordered_candidate_ids

        # --- Cursor in relevance mode: a place in the ranked list ---
        # Results ordered by an ad-hoc score page by position in the
        # ordered candidate list: after the cursor's clip (the last of the
        # page before). Ignoring the cursor here made page 2 repeat page 1.
        if after is not None and relevance_order:
            try:
                cursor_id = json.loads(_b64.urlsafe_b64decode(after + "=="))["id"]
            except (ValueError, KeyError, TypeError):
                cursor_id = after
            # A cursor whose clip left the list (it changed between pages)
            # ends the paging rather than starting it over.
            conditions.append(
                "array_position(:ordered_candidate_ids, a.asset_id)"
                " > array_position(:ordered_candidate_ids, CAST(:cursor_id AS text))"
            )
            params["cursor_id"] = cursor_id

        # --- Composite cursor: by (sort column, asset_id) ---
        if after is not None and not relevance_order:
            cursor_value = None
            cursor_id = after
            try:
                decoded = json.loads(_b64.urlsafe_b64decode(after + "=="))
                cursor_value = decoded["v"]
                cursor_id = decoded["id"]
            except Exception:
                if sort_col != "asset_id":
                    sort_col = "asset_id"

            if sort_col == "asset_id":
                conditions.append(f"a.asset_id {cmp_op} :cursor_id")
                params["cursor_id"] = cursor_id
            else:
                conditions.append(f"""(
                    -- Rows are ordered NULLS LAST: after an undated cursor only
                    -- undated rows follow; after a dated one, later dated rows
                    -- and every undated row.
                    CASE
                        WHEN :cursor_value IS NULL THEN
                            a.{sort_col} IS NULL AND a.asset_id {cmp_op} :cursor_id
                        WHEN a.{sort_col} IS NULL THEN
                            TRUE
                        ELSE
                            (a.{sort_col}, a.asset_id) {cmp_op} (:cursor_value, :cursor_id)
                    END
                )""")
                params["cursor_value"] = cursor_value
                params["cursor_id"] = cursor_id

        # --- Apply structured filters from the QuerySpec tree ---
        join_ratings = False
        join_metadata = False

        for leaf in spec.structured_filters:
            sql_frag = leaf.to_sql(params, counter)
            if sql_frag and sql_frag != "TRUE":
                conditions.append(sql_frag)
            if leaf.needs_rating_join:
                join_ratings = True
            if leaf.needs_metadata_join:
                join_metadata = True

        if join_ratings and rating_user_id:
            params["rating_user_id"] = rating_user_id
        else:
            join_ratings = False

        # --- Build query ---
        where_sql = " AND ".join(conditions) if conditions else "TRUE"

        lateral_join = ""
        if join_metadata:
            lateral_join = _corr.TAGS_JOIN  # the tags a person sees
        if spec.needs_location_join:
            from src.server.models.query_filter import LOCATION_JOIN

            lateral_join += f"\n            {LOCATION_JOIN}\n"

        rating_join_sql = ""
        if join_ratings:
            rating_join_sql = """
            LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id AND r.user_id = :rating_user_id
            """

        if relevance_order:
            # Sort by the candidate's position in the relevance-ordered
            # array. `array_position` is O(N) per row but the candidate
            # set is bounded (≤MAX_CANDIDATE_IDS=5000) so the constant
            # is small. This replaces the old chronological default
            # which was throwing the search's ranking on the floor.
            order_clause = "array_position(:ordered_candidate_ids, a.asset_id)"
        elif sort_col == "asset_id":
            order_clause = f"a.asset_id {order_dir}"
        else:
            order_clause = f"a.{sort_col} {order_dir} NULLS LAST, a.asset_id {order_dir}"

        id_sql = f"""
            SELECT a.asset_id
            FROM active_assets a
            {lateral_join}
            {rating_join_sql}
            WHERE {where_sql}
            ORDER BY {order_clause}
            LIMIT :limit
        """
        result = self._session.execute(text(id_sql).bindparams(**params))
        asset_ids = [row[0] for row in result.all()]
        if not asset_ids:
            return []
        stmt = (
            select(Asset)
            .where(Asset.asset_id.in_(asset_ids))
            .where(Asset.deleted_at.is_(None))
        )
        assets_by_id = {a.asset_id: a for a in self._session.exec(stmt).all()}
        return [assets_by_id[aid] for aid in asset_ids if aid in assets_by_id]

    def facets(self, spec: "QuerySpec", candidate_ids: list[str] | None = None,
               rating_user_id: str | None = None):
        """Facets' row (routers/facets.py): the filter values among the clips
        the spec finds (its text search's candidates, candidate_ids, already
        found), each kind in one column. A rating filter is the caller's
        ratings (rating_user_id), as the query's: someone else's favorite
        isn't theirs."""
        from src.server.models.query_filter import LOCATION_JOIN, SearchTerm, has_location_sql, location_params

        conditions: list[str] = []
        params: dict[str, object] = {}
        counter = [0]

        if candidate_ids is not None:
            conditions.append("a.asset_id = ANY(:candidate_ids)")
            params["candidate_ids"] = candidate_ids

        # --- Structured filter SQL conditions ---
        joins: list[str] = []
        needs_rating = False
        needs_metadata = False

        for leaf in spec.leaves:
            if isinstance(leaf, SearchTerm):
                continue  # handled via candidate set above
            sql_frag = leaf.to_sql(params, counter)
            conditions.append(sql_frag)
            if leaf.needs_rating_join:
                needs_rating = True
            if leaf.needs_metadata_join:
                needs_metadata = True

        # Build FROM clause with necessary JOINs
        from_clause = "active_assets a"
        joins.append(LOCATION_JOIN)  # the location counts read the effective location
        location_params(params)
        if needs_rating:
            joins.append("LEFT JOIN asset_ratings r ON r.asset_id = a.asset_id AND r.user_id = :rating_user_id")
            params["rating_user_id"] = rating_user_id
        if needs_metadata:
            from src.server.repository.corrections import TAGS_JOIN

            joins.append(TAGS_JOIN)  # the tags a person sees

        where_sql = " AND ".join(conditions) if conditions else "TRUE"
        join_sql = " ".join(joins)

        sql = f"""
            SELECT
                array_agg(DISTINCT a.camera_make) FILTER (WHERE a.camera_make IS NOT NULL) AS camera_makes,
                array_agg(DISTINCT a.camera_model) FILTER (WHERE a.camera_model IS NOT NULL) AS camera_models,
                array_agg(DISTINCT a.lens_model) FILTER (WHERE a.lens_model IS NOT NULL) AS lens_models,
                MIN(a.iso) AS iso_min,
                MAX(a.iso) AS iso_max,
                MIN(a.aperture) AS aperture_min,
                MAX(a.aperture) AS aperture_max,
                MIN(a.focal_length) AS fl_min,
                MAX(a.focal_length) AS fl_max,
                bool_or(a.media_type = 'image') AS has_images,
                bool_or(a.media_type = 'video') AS has_videos,
                COUNT(*) FILTER (WHERE {has_location_sql()}) AS gps_count,
                COUNT(*) FILTER (WHERE loc.status = 'applied' AND loc.source <> 'person') AS guess_count,
                COUNT(*) FILTER (WHERE a.face_count > 0) AS face_count
            FROM {from_clause}
            {join_sql}
            WHERE {where_sql}
        """

        return self._session.execute(text(sql).bindparams(**params)).one()
