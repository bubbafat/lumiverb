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
def test_similarity_response_defaults_model_auto_detect() -> None:
    """When model_id is not provided, the endpoint should not hardcode 'clip'.

    This is a structural check — the actual auto-detection is verified in
    test_similar_auto_detects_model (slow/integration).
    """
    import inspect
    from src.server.api.routers.similarity import find_similar

    source = inspect.getsource(find_similar)
    # The endpoint should use get_any (auto-detect) not just default to "clip"
    assert "get_any" in source, (
        "find_similar should call get_any() for model auto-detection"
    )


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

