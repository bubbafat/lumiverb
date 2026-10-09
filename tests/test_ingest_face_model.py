"""The face step says which model embedded its faces: embeddings are
compared only within one model (ADR-016 phase 3, piece 4). The worker is
src/client/cli/repair.py's, which the scheduler runs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image


@pytest.mark.fast
def test_ingest_says_which_model_embedded_its_faces(tmp_path: Path) -> None:
    from src.client.cli import repair as ingest

    Image.new("RGB", (64, 64)).save(tmp_path / "ast_a", format="JPEG")
    provider = MagicMock(model_id="insightface", model_version="antelopev2")
    provider.detect_faces.return_value = [
        MagicMock(bounding_box={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, detection_confidence=0.9, embedding=[0.1] * 512)
    ]
    client = MagicMock()
    client.post.return_value.json.return_value = {"processed": 1, "skipped": 0}
    ingest._PROVIDER = None
    with patch.object(ingest, "LumiverbClient", return_value=client), \
            patch.object(ingest, "InsightFaceProvider", return_value=provider):
        out = ingest._face_batch_worker("http://x", "t", [{"asset_id": "ast_a", "rel_path": "a.jpg"}], str(tmp_path))

    assert out["processed"] == 1, out
    assert client.post.call_args.args[0] == "/v1/assets/batch-faces"
    [body] = client.post.call_args.kwargs["json"]["items"]
    assert body["embedding_model"] == "antelopev2"
    assert body["detection_model_version"] == "antelopev2"


@pytest.mark.fast
def test_ingest_says_how_its_faces_were_found(tmp_path: Path) -> None:
    """The server refuses faces that don't say how they were found: with no
    lineage handed to it, the worker reads the server's settings."""
    from src.client.cli import repair as ingest
    from src.client.cli.producer_settings import ProducerSettings

    Image.new("RGB", (64, 64)).save(tmp_path / "ast_a", format="JPEG")
    provider = MagicMock(model_id="insightface", model_version="buffalo_l")
    provider.detect_faces.return_value = []
    client = MagicMock()
    client.post.return_value.json.return_value = {"processed": 1, "skipped": 0}
    client.get.return_value.json.return_value = {"producers": []}  # the server's settings: the registry's
    with patch.object(ingest, "LumiverbClient", return_value=client), \
            patch.object(ingest, "InsightFaceProvider", return_value=provider):
        out = ingest._face_batch_worker("http://x", "t", [{"asset_id": "ast_a", "rel_path": "a.jpg", "sha256": "abc"}],
                                        str(tmp_path))

    assert out["processed"] == 1, out
    assert client.get.call_args.args[0] == "/v1/producers"
    sent = client.post.call_args.kwargs["json"]
    assert sent["lineage"] == ProducerSettings(None).lineage("faces", None)
    assert sent["items"][0]["source_sha256"] == "abc"  # each clip's own file hash beside it
    # Handed lineage, it sends that.
    made = {"producer": "insightface", "version": "1", "settings_hash": "h", "source_sha256": None}
    with patch.object(ingest, "LumiverbClient", return_value=client), \
            patch.object(ingest, "InsightFaceProvider", return_value=provider):
        ingest._face_batch_worker("http://x", "t", [{"asset_id": "ast_a", "rel_path": "a.jpg", "sha256": "abc"}],
                                  str(tmp_path), made)
    assert client.post.call_args.kwargs["json"]["lineage"] == made
