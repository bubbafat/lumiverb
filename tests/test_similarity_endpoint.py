import pytest


@pytest.mark.fast
def test_similar_hit_model() -> None:
    """SimilarHit validates correctly."""
    from src.api.routers.similarity import SimilarHit

    hit = SimilarHit(
        asset_id="ast_001",
        rel_path="photos/a.jpg",
        thumbnail_key=None,
        proxy_key=None,
        distance=0.12,
    )
    assert hit.distance == 0.12


@pytest.mark.fast
def test_similarity_response_no_embedding() -> None:
    """SimilarityResponse with embedding_available=False has empty hits."""
    from src.api.routers.similarity import SimilarityResponse

    resp = SimilarityResponse(
        source_asset_id="ast_001",
        hits=[],
        total=0,
        embedding_available=False,
    )
    assert resp.embedding_available is False
    assert resp.hits == []

