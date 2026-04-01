"""Postgres ILIKE fallback search. Used when Quickwit is unavailable."""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def search_assets(
    session: Session,
    library_id: str | None,
    query: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    Simple Postgres fallback search using ILIKE across:
    - assets.rel_path
    - assets.camera_make
    - assets.camera_model
    - asset_metadata.data->>'description'  (most recent per asset, any model)
    - asset_metadata.data->>'tags'         (most recent per asset, any model)

    When library_id is None, searches across all libraries (cross-library).
    Returns list of dicts with asset_id, rel_path, thumbnail_key,
    proxy_key, description, tags, score=0.0 (no ranking).
    """
    like = f"%{query}%"
    lib_condition = "a.library_id = :library_id AND" if library_id else ""
    sql = text(
        f"""
        SELECT DISTINCT
            a.asset_id,
            a.library_id,
            a.rel_path,
            a.thumbnail_key,
            a.proxy_key,
            a.camera_make,
            a.camera_model,
            COALESCE(m.data->>'description', '') AS description,
            COALESCE(m.data->'tags', '[]'::jsonb) AS tags
        FROM active_assets a
        LEFT JOIN LATERAL (
            SELECT data
            FROM asset_metadata
            WHERE asset_id = a.asset_id
            ORDER BY generated_at DESC
            LIMIT 1
        ) m ON true
        WHERE {lib_condition}
              a.availability = 'online'
          AND (
              a.rel_path       ILIKE :like
           OR a.camera_make    ILIKE :like
           OR a.camera_model   ILIKE :like
           OR m.data->>'description' ILIKE :like
           OR CAST(m.data->'tags' AS TEXT) ILIKE :like
           OR m.data->>'ocr_text' ILIKE :like
           OR a.transcript_text ILIKE :like
          )
        ORDER BY a.asset_id
        LIMIT :limit OFFSET :offset
    """
    )
    params: dict = {"like": like, "limit": limit, "offset": offset}
    if library_id:
        params["library_id"] = library_id
    rows = session.execute(sql, params).fetchall()

    results: list[dict] = []
    for row in rows:
        tags = row.tags
        if isinstance(tags, str):
            import json

            try:
                tags = json.loads(tags)
            except Exception:
                tags = []
        results.append(
            {
                "asset_id": row.asset_id,
                "library_id": row.library_id,
                "rel_path": row.rel_path,
                "thumbnail_key": row.thumbnail_key,
                "proxy_key": row.proxy_key,
                "camera_make": row.camera_make,
                "camera_model": row.camera_model,
                "description": row.description,
                "tags": tags,
                "score": 0.0,
                "source": "postgres",
            }
        )
    return results

