"""Ingest's face step says which model embedded its faces: embeddings are
compared only within one model (ADR-016 phase 3, piece 4)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image


@pytest.mark.fast
def test_ingest_says_which_model_embedded_its_faces(tmp_path: Path) -> None:
    from src.client.cli import ingest

    Image.new("RGB", (64, 64)).save(tmp_path / "ast_a", format="JPEG")
    provider = MagicMock(model_id="insightface", model_version="antelopev2")
    provider.detect_faces.return_value = [
        MagicMock(bounding_box={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, detection_confidence=0.9, embedding=[0.1] * 512)
    ]
    client = MagicMock()
    with patch.object(ingest, "LumiverbClient", return_value=client), \
            patch("src.client.workers.faces.insightface_provider.InsightFaceProvider", return_value=provider):
        out = ingest._face_batch_worker("http://x", "t", [{"asset_id": "ast_a", "rel_path": "a.jpg"}], str(tmp_path))

    assert out["processed"] == 1, out
    body = client.post.call_args.kwargs["json"]
    assert body["embedding_model"] == "antelopev2"
    assert body["detection_model_version"] == "antelopev2"
