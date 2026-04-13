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
    Results are ordered by `query_builder.postgres_rank_clauses` so a
    whole-word phrase match outranks a per-token whole-word match,
    which outranks a substring-only match — keeping the fallback path
    consistent with the field-boosted Quickwit ranking. Returns list of
    dicts with asset_id, rel_path, thumbnail_key, proxy_key, description,
    tags, score=0.0 (no BM25 in the fallback).
    """
    from src.server.search.query_builder import parse_query, postgres_rank_clauses

    lib_condition = "a.library_id = :library_id AND" if library_id else ""
    rank_expr, rank_params = postgres_rank_clauses(query)

    # Build one ILIKE group per parsed term and AND them together so
    # a quoted phrase acts as a literal substring constraint instead
    # of being passed through with its quote characters intact. Free
    # tokens outside quotes become their own required group each,
    # which is a tightening vs. the old "single substring anywhere"
    # behavior — but that old behavior was already matching on the
    # full raw string, so multi-word unquoted queries also only
    # matched when the words appeared contiguously. Per-term groups
    # actually loosen that for unquoted input.
    terms = parse_query(query)
    term_patterns: list[tuple[str, str]] = []  # (bind_name, ilike_value)
    for i, term in enumerate(terms):
        term_patterns.append((f"like_{i}", f"%{term.text}%"))

    if term_patterns:
        groups = " AND ".join(
            f"""(
                  a.asset_id       ILIKE :{name}
               OR a.rel_path       ILIKE :{name}
               OR a.camera_make    ILIKE :{name}
               OR a.camera_model   ILIKE :{name}
               OR m.data->>'description' ILIKE :{name}
               OR CAST(m.data->'tags' AS TEXT) ILIKE :{name}
               OR m.data->>'ocr_text' ILIKE :{name}
               OR a.note ILIKE :{name}
               OR a.transcript_text ILIKE :{name}
              )"""
            for name, _ in term_patterns
        )
    else:
        # No parseable terms — degrade to the old raw-substring filter
        # so callers that pass opaque text (e.g. exact asset IDs) still
        # work.
        groups = """(
              a.asset_id       ILIKE :like_0
           OR a.rel_path       ILIKE :like_0
           OR a.camera_make    ILIKE :like_0
           OR a.camera_model   ILIKE :like_0
           OR m.data->>'description' ILIKE :like_0
           OR CAST(m.data->'tags' AS TEXT) ILIKE :like_0
           OR m.data->>'ocr_text' ILIKE :like_0
           OR a.note ILIKE :like_0
           OR a.transcript_text ILIKE :like_0
          )"""
        term_patterns = [("like_0", f"%{query}%")]

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
            COALESCE(m.data->'tags', '[]'::jsonb) AS tags,
            {rank_expr} AS rank
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
          AND {groups}
        ORDER BY rank ASC, a.asset_id
        LIMIT :limit OFFSET :offset
    """
    )
    params: dict = {"limit": limit, "offset": offset, **rank_params}
    for name, value in term_patterns:
        params[name] = value
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

