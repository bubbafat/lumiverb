"""Tests for batch API endpoints and proxy cache usage.

Tests the client-side batching logic and proxy cache integration.
Server-side endpoint tests require Docker (slow tests).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
class TestVisionProxyCache:
    """Test that vision backfill uses proxy cache instead of server download."""

    def test_backfill_one_uses_proxy_cache(self):
        """_backfill_one reads from proxy cache, not server."""
        from src.client.cli.ingest import _backfill_one

        mock_cache = MagicMock()
        mock_cache.get.return_value = b"fake-jpeg-bytes"

        mock_provider = MagicMock()

        with patch("src.client.cli.ingest._call_vision_ai") as mock_vision:
            mock_vision.return_value = {
                "model_id": "test",
                "model_version": "1",
                "description": "a cat",
                "tags": ["cat"],
            }

            result = _backfill_one(
                asset_id="ast_1",
                rel_path="photo.jpg",
                vision_model_id="test",
                vision_provider=mock_provider,
                proxy_cache=mock_cache,
            )

        assert result is not None
        assert result["asset_id"] == "ast_1"
        assert result["description"] == "a cat"
        mock_cache.get.assert_called_once_with("ast_1", "photo.jpg")

    def test_backfill_one_falls_back_to_server(self):
        """Falls back to server download when proxy cache misses."""
        from src.client.cli.ingest import _backfill_one

        mock_cache = MagicMock()
        mock_cache.get.return_value = None

        mock_client = MagicMock()
        mock_client.get.return_value.content = b"server-proxy"

        with patch("src.client.cli.ingest._call_vision_ai") as mock_vision:
            mock_vision.return_value = {
                "model_id": "test",
                "model_version": "1",
                "description": "from server",
                "tags": [],
            }

            result = _backfill_one(
                asset_id="ast_1",
                rel_path="photo.jpg",
                vision_model_id="test",
                vision_provider=MagicMock(),
                proxy_cache=mock_cache,
                client=mock_client,
            )

        assert result is not None
        mock_client.get.assert_called_once()

    def test_backfill_one_returns_none_on_no_proxy(self):
        """Returns None when proxy cache misses and no client fallback."""
        from src.client.cli.ingest import _backfill_one

        mock_cache = MagicMock()
        mock_cache.get.return_value = None

        result = _backfill_one(
            asset_id="ast_1",
            rel_path="photo.jpg",
            vision_model_id="test",
            vision_provider=MagicMock(),
            proxy_cache=mock_cache,
        )

        assert result is None

    def test_backfill_one_returns_none_on_vision_failure(self):
        """Returns None when vision AI returns no result."""
        from src.client.cli.ingest import _backfill_one

        mock_cache = MagicMock()
        mock_cache.get.return_value = b"fake-jpeg"

        with patch("src.client.cli.ingest._call_vision_ai") as mock_vision:
            mock_vision.return_value = None

            result = _backfill_one(
                asset_id="ast_1",
                rel_path="photo.jpg",
                vision_model_id="test",
                vision_provider=MagicMock(),
                proxy_cache=mock_cache,
            )

        assert result is None


@pytest.mark.fast
class TestEmbedReturnResult:
    """Test that _repair_embed_one returns result dict instead of posting."""

    def test_returns_embedding_dict(self):
        """_repair_embed_one returns dict with vector on success."""
        from src.client.cli.repair import _repair_embed_one
        from PIL import Image as PILImage
        import io as _io

        # Create a real JPEG so PIL.open succeeds
        buf = _io.BytesIO()
        PILImage.new("RGB", (10, 10), color=(128, 128, 128)).save(buf, format="JPEG")
        jpeg_bytes = buf.getvalue()

        mock_cache = MagicMock()
        mock_cache.get.return_value = jpeg_bytes

        mock_clip = MagicMock()
        mock_clip.model_id = "clip"
        mock_clip.model_version = "v1"
        mock_clip.embed_image.return_value = [0.5] * 512

        result = _repair_embed_one(
            asset_id="ast_1",
            rel_path="photo.jpg",
            clip_provider=mock_clip,
            proxy_cache=mock_cache,
        )

        assert result is not None
        assert result["asset_id"] == "ast_1"
        assert result["model_id"] == "clip"
        assert len(result["vector"]) == 512

    def test_returns_none_on_no_proxy(self):
        """Returns None when no proxy available."""
        from src.client.cli.repair import _repair_embed_one

        mock_cache = MagicMock()
        mock_cache.get.return_value = None

        result = _repair_embed_one(
            asset_id="ast_1",
            rel_path="photo.jpg",
            clip_provider=MagicMock(),
            proxy_cache=mock_cache,
        )

        assert result is None


@pytest.mark.fast
class TestBatchEndpointModels:
    """Verify batch request/response models are importable and correct."""

    def test_batch_vision_model(self):
        from src.server.api.routers.assets import BatchVisionRequest, BatchVisionItem
        req = BatchVisionRequest(items=[
            BatchVisionItem(asset_id="a1", model_id="m", description="d", tags=["t"]),
        ])
        assert len(req.items) == 1
        assert req.items[0].model_version == "1"  # default

    def test_batch_embedding_model(self):
        from src.server.api.routers.assets import BatchEmbeddingRequest, BatchEmbeddingItem
        req = BatchEmbeddingRequest(items=[
            BatchEmbeddingItem(asset_id="a1", model_id="clip", model_version="v1", vector=[0.1] * 10),
        ])
        assert len(req.items) == 1
        assert len(req.items[0].vector) == 10


@pytest.mark.fast
class TestMissingOcrCondition:
    """Verify the missing_ocr SQL condition uses has_text flag."""

    def test_missing_ocr_checks_has_text(self):
        """missing_ocr condition should check has_text IS NULL, not ocr_text."""
        from src.server.repository.tenant import MISSING_CONDITIONS
        cond = MISSING_CONDITIONS["missing_ocr"]
        assert "has_text" in cond
        assert "ocr_text" not in cond
