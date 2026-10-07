import pytest


@pytest.mark.fast
def test_query_response_model() -> None:
    """QueryResponse carries search hits with their SearchContext."""
    from src.server.api.routers.query import QueryItem, QueryResponse, SearchContext

    item = QueryItem(
        asset_id="ast_001",
        library_id="lib_001",
        library_name="Photos",
        rel_path="photos/test.jpg",
        file_size=1000,
        media_type="image",
        search_context=SearchContext(score=1.5, hit_type="asset", snippet="A sunset."),
    )
    resp = QueryResponse(items=[item], total_estimate=1, search_source="quickwit")
    assert resp.total_estimate == 1
    assert resp.items[0].search_context.score == 1.5


@pytest.mark.fast
def test_postgres_search_empty_query_returns_no_results() -> None:
    """search_assets with no matching query returns empty list."""
    from unittest.mock import MagicMock

    from src.server.search.postgres_search import search_assets

    mock_session = MagicMock()
    mock_session.execute.return_value.fetchall.return_value = []
    results = search_assets(mock_session, "lib_001", "xyzzy_nonexistent", limit=10)
    assert results == []

