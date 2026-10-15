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
    resp = QueryResponse(items=[item], total_estimate=1, search_source="postgres")
    assert resp.total_estimate == 1
    assert resp.items[0].search_context.score == 1.5


@pytest.mark.fast
@pytest.mark.parametrize("typed", ["", "   ", "!!!", "%", "_", '""'])
def test_a_search_without_a_word_finds_nothing_and_reads_nothing(typed: str) -> None:
    """Search terms with no word in them find nothing (search_source "none"),
    without a query: never everything."""
    from unittest.mock import MagicMock

    from src.server.api.routers.query import _run_text_search
    from src.server.models.query_filter import SearchTerm

    session = MagicMock()
    scores, contexts, source = _run_text_search(session, [SearchTerm(q=typed)], ["lib_001"], limit=10)
    assert (scores, contexts, source) == ({}, {}, "none")
    session.execute.assert_not_called()
