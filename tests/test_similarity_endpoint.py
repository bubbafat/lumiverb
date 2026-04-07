import pytest


@pytest.mark.fast
def test_similar_hit_model() -> None:
    """SimilarHit validates correctly."""
    from src.server.api.routers.similarity import SimilarHit

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
    from src.server.api.routers.similarity import SimilarityResponse

    resp = SimilarityResponse(
        source_asset_id="ast_001",
        hits=[],
        total=0,
        embedding_available=False,
    )
    assert resp.embedding_available is False
    assert resp.hits == []


@pytest.mark.fast
def test_image_similarity_models() -> None:
    """ImageSimilarityRequest and ImageSimilarityResponse validate correctly."""
    from src.server.api.routers.similarity import (
        CameraSpec,
        ImageSimilarityRequest,
        ImageSimilarityResponse,
        SimilarHit,
    )

    cam = CameraSpec(make="FUJIFILM", model="X100V")
    req = ImageSimilarityRequest(
        library_id="lib_123",
        image_b64="dGVzdA==",
        limit=5,
        offset=2,
        from_ts=1700000000.0,
        to_ts=1800000000.0,
        asset_types=["image"],
        cameras=[cam],
    )
    assert req.library_id == "lib_123"
    assert req.asset_types == ["image"]
    assert req.cameras[0].make == "FUJIFILM"

    hit = SimilarHit(
        asset_id="ast_1",
        rel_path="path.jpg",
        thumbnail_key=None,
        proxy_key=None,
        distance=0.1,
    )
    resp = ImageSimilarityResponse(hits=[hit], total=1)
    assert resp.total == 1
    assert resp.hits[0].asset_id == "ast_1"

