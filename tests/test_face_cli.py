"""Fast tests for face detection CLI integration (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
def test_detect_faces_returns_payload() -> None:
    """_detect_faces returns submission payload from provider detections."""
    from src.workers.faces.insightface_provider import FaceDetection

    mock_provider = MagicMock()
    mock_provider.model_id = "insightface"
    mock_provider.model_version = "buffalo_l"
    mock_provider.detect_faces.return_value = [
        FaceDetection(
            bounding_box={"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
            detection_confidence=0.95,
            embedding=[0.1] * 512,
        ),
    ]

    from src.cli.ingest import _detect_faces

    jpeg_bytes = _make_jpeg_bytes()
    result = _detect_faces(jpeg_bytes, mock_provider)

    assert result is not None
    assert result["detection_model"] == "insightface"
    assert len(result["faces"]) == 1
    assert result["faces"][0]["detection_confidence"] == 0.95


@pytest.mark.fast
def test_detect_faces_returns_none_when_no_provider() -> None:
    """_detect_faces returns None when provider is None."""
    from src.cli.ingest import _detect_faces

    result = _detect_faces(b"fake", None)
    assert result is None


@pytest.mark.fast
def test_detect_faces_returns_none_on_error() -> None:
    """_detect_faces returns None on provider error (non-blocking)."""
    mock_provider = MagicMock()
    mock_provider.detect_faces.side_effect = RuntimeError("model crash")

    from src.cli.ingest import _detect_faces

    result = _detect_faces(_make_jpeg_bytes(), mock_provider)
    assert result is None


@pytest.mark.fast
def test_repair_types_includes_faces() -> None:
    """REPAIR_TYPES includes 'faces'."""
    from src.cli.repair import REPAIR_TYPES

    assert "faces" in REPAIR_TYPES


@pytest.mark.fast
def test_page_missing_accepts_missing_faces() -> None:
    """_page_missing passes missing_faces param to API."""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [], "next_cursor": None}
    mock_client.get.return_value = mock_resp

    from src.cli.repair import _page_missing

    _page_missing(mock_client, "lib_123", missing_faces=True)

    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["missing_faces"] == "true"


@pytest.mark.fast
def test_detect_faces_empty_image() -> None:
    """_detect_faces with no faces returns empty faces list."""
    mock_provider = MagicMock()
    mock_provider.model_id = "insightface"
    mock_provider.model_version = "buffalo_l"
    mock_provider.detect_faces.return_value = []

    from src.cli.ingest import _detect_faces

    result = _detect_faces(_make_jpeg_bytes(), mock_provider)
    assert result is not None
    assert result["faces"] == []


@pytest.mark.fast
def test_face_batch_worker_processes_assets() -> None:
    """_face_batch_worker downloads proxies, runs detection, and posts results."""
    from unittest.mock import MagicMock, patch, call

    mock_client_instance = MagicMock()
    # Simulate proxy download returning JPEG bytes
    proxy_resp = MagicMock()
    proxy_resp.status_code = 200
    proxy_resp.content = _make_jpeg_bytes()
    proxy_resp.close = MagicMock()
    # Simulate face POST returning 201
    post_resp = MagicMock()
    post_resp.status_code = 201
    mock_client_instance.get.return_value = proxy_resp
    mock_client_instance.post.return_value = post_resp

    from src.workers.faces.insightface_provider import FaceDetection

    mock_provider = MagicMock()
    mock_provider.model_id = "insightface"
    mock_provider.model_version = "buffalo_l"
    mock_provider.detect_faces.return_value = [
        FaceDetection(
            bounding_box={"x": 0.1, "y": 0.2, "w": 0.15, "h": 0.2},
            detection_confidence=0.95,
            embedding=[0.1] * 512,
        ),
    ]
    mock_provider.ensure_loaded = MagicMock()

    batch = [
        {"asset_id": "ast_001"},
        {"asset_id": "ast_002"},
        {"asset_id": "ast_003"},
    ]

    with patch("src.cli.repair.LumiverbClient", return_value=mock_client_instance) as mock_cls, \
         patch("src.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.cli.repair import _face_batch_worker
        result = _face_batch_worker("http://localhost", "test-token", batch)

    assert result["processed"] == 3
    assert result["failed"] == 0
    assert result["skipped"] == 0
    # Should have posted faces for each asset
    assert mock_client_instance.post.call_count == 3


@pytest.mark.fast
def test_face_batch_worker_skips_missing_proxy() -> None:
    """_face_batch_worker skips assets where proxy returns non-200."""
    from unittest.mock import MagicMock, patch

    mock_client_instance = MagicMock()
    proxy_resp = MagicMock()
    proxy_resp.status_code = 404
    mock_client_instance.get.return_value = proxy_resp

    mock_provider = MagicMock()
    mock_provider.ensure_loaded = MagicMock()

    batch = [{"asset_id": "ast_missing"}]

    with patch("src.cli.repair.LumiverbClient", return_value=mock_client_instance), \
         patch("src.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.cli.repair import _face_batch_worker
        result = _face_batch_worker("http://localhost", "test-token", batch)

    assert result["skipped"] == 1
    assert result["processed"] == 0


@pytest.mark.fast
def test_face_batch_worker_counts_failures() -> None:
    """_face_batch_worker counts exceptions as failures."""
    from unittest.mock import MagicMock, patch

    mock_client_instance = MagicMock()
    proxy_resp = MagicMock()
    proxy_resp.status_code = 200
    proxy_resp.content = _make_jpeg_bytes()
    proxy_resp.close = MagicMock()
    mock_client_instance.get.return_value = proxy_resp

    mock_provider = MagicMock()
    mock_provider.ensure_loaded = MagicMock()
    mock_provider.detect_faces.side_effect = RuntimeError("model crash")

    batch = [{"asset_id": "ast_crash"}]

    with patch("src.cli.repair.LumiverbClient", return_value=mock_client_instance), \
         patch("src.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.cli.repair import _face_batch_worker
        result = _face_batch_worker("http://localhost", "test-token", batch)

    assert result["failed"] == 1
    assert result["processed"] == 0


def _make_jpeg_bytes() -> bytes:
    """Create minimal valid JPEG bytes for testing."""
    from PIL import Image as PILImage
    import io
    img = PILImage.new("RGB", (100, 100), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()
