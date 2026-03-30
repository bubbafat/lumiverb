"""Fast unit tests for face detection models and provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
def test_face_submit_request_model() -> None:
    """FaceSubmitRequest validates with default model fields."""
    from src.api.routers.assets import FaceSubmitRequest, FaceDetectionItem

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
    from src.api.routers.assets import FaceDetectionItem

    item = FaceDetectionItem(
        bounding_box={"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
        detection_confidence=0.8,
    )
    assert item.embedding is None


@pytest.mark.fast
def test_face_submit_response_model() -> None:
    """FaceSubmitResponse serializes correctly."""
    from src.api.routers.assets import FaceSubmitResponse

    resp = FaceSubmitResponse(face_count=2, face_ids=["face_a", "face_b"])
    assert resp.face_count == 2
    assert len(resp.face_ids) == 2


@pytest.mark.fast
def test_face_list_response_model() -> None:
    """FaceListResponse with person=None."""
    from src.api.routers.assets import FaceListResponse, FaceListItem

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
    from src.api.routers.assets import RepairSummary

    summary = RepairSummary(
        total_assets=100,
        missing_faces=25,
    )
    assert summary.missing_faces == 25
    assert summary.missing_embeddings == 0


@pytest.mark.fast
def test_insightface_provider_properties() -> None:
    """InsightFaceProvider has correct model_id and model_version."""
    from src.workers.faces.insightface_provider import InsightFaceProvider

    provider = InsightFaceProvider()
    assert provider.model_id == "insightface"
    assert provider.model_version == "buffalo_l"


@pytest.mark.fast
def test_insightface_provider_detect_faces_mocked() -> None:
    """InsightFaceProvider.detect_faces returns FaceDetection objects from mocked InsightFace."""
    import numpy as np
    from PIL import Image as PILImage

    from src.workers.faces.insightface_provider import InsightFaceProvider

    # Create a small test image
    img = PILImage.new("RGB", (640, 480), color=(128, 128, 128))

    # Mock the InsightFace FaceAnalysis app
    mock_face = MagicMock()
    mock_face.bbox = [64, 48, 192, 192]  # pixels: x1, y1, x2, y2
    mock_face.det_score = 0.95
    mock_face.normed_embedding = np.random.randn(512).astype(np.float32)
    # Normalize
    mock_face.normed_embedding = mock_face.normed_embedding / np.linalg.norm(mock_face.normed_embedding)

    mock_app = MagicMock()
    mock_app.get.return_value = [mock_face]

    provider = InsightFaceProvider()
    provider._app = mock_app  # Inject mock

    results = provider.detect_faces(img)

    assert len(results) == 1
    r = results[0]
    assert 0.0 <= r.bounding_box["x"] <= 1.0
    assert 0.0 <= r.bounding_box["y"] <= 1.0
    assert 0.0 <= r.bounding_box["w"] <= 1.0
    assert 0.0 <= r.bounding_box["h"] <= 1.0
    assert r.detection_confidence == pytest.approx(0.95)
    assert len(r.embedding) == 512


@pytest.mark.fast
def test_insightface_provider_no_faces() -> None:
    """InsightFaceProvider returns empty list when no faces detected."""
    from PIL import Image as PILImage

    from src.workers.faces.insightface_provider import InsightFaceProvider

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
    from PIL import Image as PILImage

    from src.workers.faces.insightface_provider import InsightFaceProvider

    img = PILImage.new("RGB", (640, 480))

    mock_face = MagicMock()
    mock_face.bbox = [10, 10, 100, 100]
    mock_face.det_score = 0.9
    mock_face.normed_embedding = None  # No embedding

    mock_app = MagicMock()
    mock_app.get.return_value = [mock_face]

    provider = InsightFaceProvider()
    provider._app = mock_app

    results = provider.detect_faces(img)
    assert results == []


@pytest.mark.fast
def test_face_detection_dataclass() -> None:
    """FaceDetection dataclass holds expected fields."""
    from src.workers.faces.insightface_provider import FaceDetection

    fd = FaceDetection(
        bounding_box={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        detection_confidence=0.99,
        embedding=[0.0] * 512,
    )
    assert fd.bounding_box["x"] == 0.1
    assert fd.detection_confidence == 0.99
    assert len(fd.embedding) == 512


@pytest.mark.fast
def test_face_model_has_detection_model_fields() -> None:
    """Face SQLModel has detection_model and detection_model_version fields."""
    from src.models.tenant import Face

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
    from src.models.tenant import Person

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
    from src.models.tenant import Asset

    a = Asset(
        asset_id="test",
        library_id="lib",
        rel_path="test.jpg",
        file_size=1000,
        media_type="image",
    )
    assert a.face_count is None
