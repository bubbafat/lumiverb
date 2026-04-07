"""Fast tests for face detection CLI integration (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
def test_repair_types_includes_faces() -> None:
    """REPAIR_TYPES includes 'faces'."""
    from src.client.cli.repair import REPAIR_TYPES

    assert "faces" in REPAIR_TYPES


@pytest.mark.fast
def test_page_missing_accepts_missing_faces() -> None:
    """_page_missing passes missing_faces param to API."""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"items": [], "next_cursor": None}
    mock_client.get.return_value = mock_resp

    from src.client.cli.repair import _page_missing

    _page_missing(mock_client, "lib_123", missing_faces=True)

    call_args = mock_client.get.call_args
    assert call_args[1]["params"]["missing_faces"] == "true"


@pytest.mark.fast
def test_face_batch_worker_processes_assets() -> None:
    """_face_batch_worker downloads proxies, runs detection, and posts results."""
    from unittest.mock import MagicMock, patch, call

    mock_http_client = MagicMock()
    # Simulate proxy download returning JPEG bytes
    proxy_resp = MagicMock()
    proxy_resp.status_code = 200
    proxy_resp.content = _make_jpeg_bytes()
    proxy_resp.close = MagicMock()
    mock_http_client.get.return_value = proxy_resp

    mock_client_instance = MagicMock()
    mock_client_instance._client = mock_http_client
    mock_client_instance._url = lambda path: f"http://localhost{path}"
    # POST for face submission goes through the wrapper
    post_resp = MagicMock()
    post_resp.status_code = 201
    mock_client_instance.post.return_value = post_resp

    from src.client.workers.faces.insightface_provider import FaceDetection

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

    with patch("src.client.cli.repair.LumiverbClient", return_value=mock_client_instance), \
         patch("src.client.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.client.cli.repair import _face_batch_worker
        result = _face_batch_worker("http://localhost", "test-token", batch)

    assert result["processed"] == 3
    assert result["failed"] == 0
    assert result["skipped"] == 0
    assert mock_client_instance.post.call_count == 3


@pytest.mark.fast
def test_face_batch_worker_skips_missing_proxy() -> None:
    """_face_batch_worker skips assets where proxy returns non-200."""
    from unittest.mock import MagicMock, patch

    mock_http_client = MagicMock()
    proxy_resp = MagicMock()
    proxy_resp.status_code = 404
    proxy_resp.close = MagicMock()
    mock_http_client.get.return_value = proxy_resp

    mock_client_instance = MagicMock()
    mock_client_instance._client = mock_http_client
    mock_client_instance._url = lambda path: f"http://localhost{path}"

    mock_provider = MagicMock()
    mock_provider.ensure_loaded = MagicMock()

    batch = [{"asset_id": "ast_missing"}]

    with patch("src.client.cli.repair.LumiverbClient", return_value=mock_client_instance), \
         patch("src.client.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.client.cli.repair import _face_batch_worker
        result = _face_batch_worker("http://localhost", "test-token", batch)

    assert result["skipped"] == 1
    assert result["processed"] == 0


@pytest.mark.fast
def test_face_batch_worker_counts_failures() -> None:
    """_face_batch_worker counts exceptions as failures."""
    from unittest.mock import MagicMock, patch

    mock_http_client = MagicMock()
    proxy_resp = MagicMock()
    proxy_resp.status_code = 200
    proxy_resp.content = _make_jpeg_bytes()
    proxy_resp.close = MagicMock()
    mock_http_client.get.return_value = proxy_resp

    mock_client_instance = MagicMock()
    mock_client_instance._client = mock_http_client
    mock_client_instance._url = lambda path: f"http://localhost{path}"

    mock_provider = MagicMock()
    mock_provider.ensure_loaded = MagicMock()
    mock_provider.detect_faces.side_effect = RuntimeError("model crash")

    batch = [{"asset_id": "ast_crash"}]

    with patch("src.client.cli.repair.LumiverbClient", return_value=mock_client_instance), \
         patch("src.client.cli.repair.InsightFaceProvider", return_value=mock_provider):
        from src.client.cli.repair import _face_batch_worker
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
