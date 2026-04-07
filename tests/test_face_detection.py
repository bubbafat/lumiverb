"""Fast unit tests for face detection models and provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
def test_face_submit_request_model() -> None:
    """FaceSubmitRequest validates with default model fields."""
    from src.server.api.routers.assets import FaceSubmitRequest, FaceDetectionItem

    req = FaceSubmitRequest(
        faces=[
            FaceDetectionItem(
                bounding_box={"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
                detection_confidence=0.97,
                embedding=[0.1] * 512,
            )
        ]
    )
    assert req.detection_model == "insightface"
    assert req.detection_model_version == "buffalo_l"
    assert len(req.faces) == 1


@pytest.mark.fast
def test_face_submit_request_no_embedding() -> None:
    """FaceDetectionItem allows None embedding."""
    from src.server.api.routers.assets import FaceDetectionItem

    item = FaceDetectionItem(
        bounding_box={"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
        detection_confidence=0.8,
    )
    assert item.embedding is None


@pytest.mark.fast
def test_face_submit_response_model() -> None:
    """FaceSubmitResponse serializes correctly."""
    from src.server.api.routers.assets import FaceSubmitResponse

    resp = FaceSubmitResponse(face_count=2, face_ids=["face_a", "face_b"])
    assert resp.face_count == 2
    assert len(resp.face_ids) == 2


@pytest.mark.fast
def test_face_list_response_model() -> None:
    """FaceListResponse with person=None."""
    from src.server.api.routers.assets import FaceListResponse, FaceListItem

    resp = FaceListResponse(
        faces=[
            FaceListItem(
                face_id="face_a",
                bounding_box={"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
                detection_confidence=0.97,
                person=None,
            )
        ]
    )
    assert len(resp.faces) == 1
    assert resp.faces[0].person is None


@pytest.mark.fast
def test_repair_summary_includes_missing_faces() -> None:
    """RepairSummary model has missing_faces field."""
    from src.server.api.routers.assets import RepairSummary

    summary = RepairSummary(
        total_assets=100,
        missing_faces=25,
    )
    assert summary.missing_faces == 25
    assert summary.missing_embeddings == 0


@pytest.mark.fast
def test_insightface_provider_properties() -> None:
    """InsightFaceProvider has correct model_id and model_version."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    provider = InsightFaceProvider()
    assert provider.model_id == "insightface"
    assert provider.model_version == "buffalo_l"


def _make_sharp_image(width: int = 640, height: int = 480):
    """Create a test image with high-frequency detail (passes sharpness gate)."""
    import numpy as np
    from PIL import Image as PILImage

    # Checkerboard pattern — high Laplacian variance
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[::2, ::2] = 255
    arr[1::2, 1::2] = 255
    return PILImage.fromarray(arr)


def _make_mock_face(bbox, det_score=0.95, embedding=True):
    """Create a mock InsightFace detection result."""
    import numpy as np

    face = MagicMock()
    face.bbox = bbox
    face.det_score = det_score
    if embedding:
        emb = np.random.randn(512).astype(np.float32)
        face.normed_embedding = emb / np.linalg.norm(emb)
    else:
        face.normed_embedding = None
    return face


@pytest.mark.fast
def test_insightface_provider_detect_faces_mocked() -> None:
    """InsightFaceProvider.detect_faces returns FaceDetection objects from mocked InsightFace."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(640, 480)

    # Large face covering ~30% of image — passes all gates
    mock_face = _make_mock_face([64, 48, 320, 336])

    mock_app = MagicMock()
    mock_app.get.return_value = [mock_face]

    provider = InsightFaceProvider()
    provider._app = mock_app

    results = provider.detect_faces(img)

    assert len(results) == 1
    r = results[0]
    assert 0.0 <= r.bounding_box["x"] <= 1.0
    assert 0.0 <= r.bounding_box["y"] <= 1.0
    assert 0.0 <= r.bounding_box["w"] <= 1.0
    assert 0.0 <= r.bounding_box["h"] <= 1.0
    assert r.detection_confidence == pytest.approx(0.95)
    assert len(r.embedding) == 512
    assert r.sharpness > 0


@pytest.mark.fast
def test_insightface_provider_no_faces() -> None:
    """InsightFaceProvider returns empty list when no faces detected."""
    from PIL import Image as PILImage

    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = PILImage.new("RGB", (100, 100), color=(255, 255, 255))

    mock_app = MagicMock()
    mock_app.get.return_value = []

    provider = InsightFaceProvider()
    provider._app = mock_app

    results = provider.detect_faces(img)
    assert results == []


@pytest.mark.fast
def test_insightface_provider_skips_face_without_embedding() -> None:
    """Faces with normed_embedding=None are skipped."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(640, 480)

    mock_face = _make_mock_face([64, 48, 320, 336], embedding=False)

    mock_app = MagicMock()
    mock_app.get.return_value = [mock_face]

    provider = InsightFaceProvider()
    provider._app = mock_app

    results = provider.detect_faces(img)
    assert results == []


@pytest.mark.fast
def test_face_detection_dataclass() -> None:
    """FaceDetection dataclass holds expected fields."""
    from src.client.workers.faces.insightface_provider import FaceDetection

    fd = FaceDetection(
        bounding_box={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        detection_confidence=0.99,
        embedding=[0.0] * 512,
    )
    assert fd.bounding_box["x"] == 0.1
    assert fd.detection_confidence == 0.99
    assert len(fd.embedding) == 512
    assert fd.sharpness == 0.0  # default


# ---------------------------------------------------------------------------
# Quality gate tests
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_gate_low_confidence_dropped() -> None:
    """Faces below MIN_DETECTION_CONFIDENCE are dropped."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(640, 480)
    mock_app = MagicMock()
    mock_app.get.return_value = [
        _make_mock_face([64, 48, 320, 336], det_score=0.3),  # below 0.5
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    assert provider.detect_faces(img) == []


@pytest.mark.fast
def test_gate_tiny_pixel_width_dropped() -> None:
    """Faces narrower than MIN_FACE_PIXELS are dropped."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(640, 480)
    mock_app = MagicMock()
    # 30px wide face — below 40px threshold
    mock_app.get.return_value = [
        _make_mock_face([100, 100, 130, 160], det_score=0.9),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    assert provider.detect_faces(img) == []


@pytest.mark.fast
def test_gate_tiny_area_fraction_dropped() -> None:
    """Faces below MIN_BBOX_AREA_FRACTION of image are dropped."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    # 2000x2000 image, face is 42x42 px → area = 0.00044 (< 0.003)
    img = _make_sharp_image(2000, 2000)
    mock_app = MagicMock()
    mock_app.get.return_value = [
        _make_mock_face([100, 100, 142, 142], det_score=0.9),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    assert provider.detect_faces(img) == []


@pytest.mark.fast
def test_gate_blurry_face_dropped() -> None:
    """Faces with low Laplacian sharpness are dropped."""
    import numpy as np
    from PIL import Image as PILImage
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    # Solid-color image — Laplacian variance ≈ 0
    img = PILImage.new("RGB", (640, 480), color=(128, 128, 128))
    mock_app = MagicMock()
    mock_app.get.return_value = [
        _make_mock_face([64, 48, 320, 336], det_score=0.9),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    assert provider.detect_faces(img) == []


@pytest.mark.fast
def test_gate_relative_size_drops_tiny_face() -> None:
    """A face < 15% area of the largest face in the same image is dropped."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(1000, 1000)
    mock_app = MagicMock()
    mock_app.get.return_value = [
        # Big face: 400x400 px = 16% of image
        _make_mock_face([100, 100, 500, 500], det_score=0.95),
        # Small face: 50x50 px = 0.25% of image, ratio to big = 1.6%
        _make_mock_face([800, 800, 850, 850], det_score=0.9),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    results = provider.detect_faces(img)
    assert len(results) == 1
    assert results[0].detection_confidence == pytest.approx(0.95)


@pytest.mark.fast
def test_gate_similar_size_faces_kept() -> None:
    """Two similarly-sized faces both pass the relative size gate."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(1000, 1000)
    mock_app = MagicMock()
    mock_app.get.return_value = [
        # Face 1: 200x200 px
        _make_mock_face([50, 50, 250, 250], det_score=0.95),
        # Face 2: 180x180 px — ratio ≈ 81% of face 1
        _make_mock_face([500, 500, 680, 680], det_score=0.92),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    results = provider.detect_faces(img)
    assert len(results) == 2


@pytest.mark.fast
def test_gate_all_pass() -> None:
    """A high-confidence, large, sharp face passes all gates."""
    from src.client.workers.faces.insightface_provider import InsightFaceProvider

    img = _make_sharp_image(640, 480)
    mock_app = MagicMock()
    mock_app.get.return_value = [
        _make_mock_face([64, 48, 320, 336], det_score=0.98),
    ]

    provider = InsightFaceProvider()
    provider._app = mock_app
    results = provider.detect_faces(img)
    assert len(results) == 1
    assert results[0].sharpness > 0


@pytest.mark.fast
def test_face_model_has_detection_model_fields() -> None:
    """Face SQLModel has detection_model and detection_model_version fields."""
    from src.server.models.tenant import Face

    f = Face(
        face_id="face_test",
        asset_id="asset_test",
        detection_model="insightface",
        detection_model_version="buffalo_l",
    )
    assert f.detection_model == "insightface"
    assert f.detection_model_version == "buffalo_l"


@pytest.mark.fast
def test_person_model_has_clustering_fields() -> None:
    """Person SQLModel has centroid_vector, confirmation_count, representative_face_id."""
    from src.server.models.tenant import Person

    p = Person(
        person_id="person_test",
        display_name="Test Person",
    )
    assert p.confirmation_count == 0
    assert p.representative_face_id is None
    # centroid_vector defaults to None
    assert p.centroid_vector is None


@pytest.mark.fast
def test_asset_model_has_face_count() -> None:
    """Asset model includes face_count field."""
    from src.server.models.tenant import Asset

    a = Asset(
        asset_id="test",
        library_id="lib",
        rel_path="test.jpg",
        file_size=1000,
        media_type="image",
    )
    assert a.face_count is None
