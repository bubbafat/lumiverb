import pytest


@pytest.mark.fast
def test_search_response_model() -> None:
    """SearchResponse and SearchHit validate correctly."""
    from src.server.api.routers.search import SearchHit, SearchResponse

    hit = SearchHit(
        asset_id="ast_001",
        rel_path="photos/test.jpg",
        thumbnail_key="t/l/thumbnails/00/ast_001.jpg",
        proxy_key=None,
        description="A sunset.",
        tags=["sunset"],
        score=1.5,
        source="quickwit",
    )
    resp = SearchResponse(query="sunset", hits=[hit], total=1, source="quickwit")
    assert resp.total == 1
    assert resp.hits[0].score == 1.5


@pytest.mark.fast
def test_postgres_search_empty_query_returns_no_results() -> None:
    """search_assets with no matching query returns empty list."""
    from unittest.mock import MagicMock

    from src.server.search.postgres_search import search_assets

    mock_session = MagicMock()
    mock_session.execute.return_value.fetchall.return_value = []
    results = search_assets(mock_session, "lib_001", "xyzzy_nonexistent", limit=10)
    assert results == []

