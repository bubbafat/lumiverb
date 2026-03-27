"""Tests for ArtifactStore implementations (LocalArtifactStore, RemoteArtifactStore, factory)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.storage.artifact_store import (
    ArtifactRef,
    LocalArtifactStore,
    RemoteArtifactStore,
    get_artifact_store,
)
from src.storage.local import LocalStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_ID = "tnt_test"
LIBRARY_ID = "lib_test"
ASSET_ID = "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
REL_PATH = "photos/summer/beach.jpg"
SAMPLE_BYTES = b"\xff\xd8\xff" + b"\x00" * 100  # fake JPEG header + padding


def _make_local_store(tmp_path: Path) -> LocalArtifactStore:
    storage = LocalStorage(data_dir=str(tmp_path))
    return LocalArtifactStore(storage=storage, tenant_id=TENANT_ID)


# ---------------------------------------------------------------------------
# LocalArtifactStore — write_artifact
# ---------------------------------------------------------------------------


def test_local_write_artifact_proxy(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    ref = store.write_artifact(
        "proxy", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
        width=2048, height=1365,
    )

    assert isinstance(ref, ArtifactRef)
    assert "proxies" in ref.key
    assert ASSET_ID in ref.key
    assert ref.key.endswith(".webp")
    assert ref.sha256 == hashlib.sha256(SAMPLE_BYTES).hexdigest()
    assert (tmp_path / ref.key).exists()
    assert (tmp_path / ref.key).read_bytes() == SAMPLE_BYTES


def test_local_write_artifact_thumbnail(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    ref = store.write_artifact(
        "thumbnail", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
    )

    assert "thumbnails" in ref.key
    assert ASSET_ID in ref.key
    assert ref.key.endswith(".webp")
    assert ref.sha256 == hashlib.sha256(SAMPLE_BYTES).hexdigest()
    assert (tmp_path / ref.key).read_bytes() == SAMPLE_BYTES


def test_local_write_artifact_video_preview(tmp_path: Path) -> None:
    mp4_bytes = b"\x00\x00\x00\x20ftyp" + b"\x00" * 50  # fake MP4 header
    store = _make_local_store(tmp_path)
    ref = store.write_artifact(
        "video_preview", ASSET_ID, mp4_bytes,
        library_id=LIBRARY_ID, rel_path="videos/clip.mp4",
    )

    assert "previews" in ref.key
    assert ref.key.endswith(".mp4")
    assert ref.sha256 == hashlib.sha256(mp4_bytes).hexdigest()
    assert (tmp_path / ref.key).read_bytes() == mp4_bytes


def test_local_write_artifact_scene_rep(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    rep_frame_ms = 12345
    ref = store.write_artifact(
        "scene_rep", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
        rep_frame_ms=rep_frame_ms,
    )

    assert "scenes" in ref.key
    assert str(rep_frame_ms) in ref.key or f"{rep_frame_ms:010d}" in ref.key
    assert ref.key.endswith(".jpg")
    assert ref.sha256 == hashlib.sha256(SAMPLE_BYTES).hexdigest()
    assert (tmp_path / ref.key).read_bytes() == SAMPLE_BYTES


def test_local_write_artifact_scene_rep_requires_rep_frame_ms(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    with pytest.raises(ValueError, match="rep_frame_ms"):
        store.write_artifact(
            "scene_rep", ASSET_ID, SAMPLE_BYTES,
            library_id=LIBRARY_ID, rel_path=REL_PATH,
        )


def test_local_write_artifact_unknown_type_raises(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    with pytest.raises(ValueError, match="Unknown artifact_type"):
        store.write_artifact(
            "unknown", ASSET_ID, SAMPLE_BYTES,
            library_id=LIBRARY_ID, rel_path=REL_PATH,
        )


# ---------------------------------------------------------------------------
# LocalArtifactStore — read_artifact (roundtrip)
# ---------------------------------------------------------------------------


def test_local_read_artifact_roundtrip(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    ref = store.write_artifact(
        "proxy", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
    )
    # asset_id and artifact_type are accepted but ignored in local mode
    result = store.read_artifact(ref.key, asset_id=ASSET_ID, artifact_type="proxy")
    assert result == SAMPLE_BYTES


def test_local_read_artifact_missing_raises(tmp_path: Path) -> None:
    store = _make_local_store(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read_artifact(
            "nonexistent/key.jpg",
            asset_id=ASSET_ID,
            artifact_type="proxy",
        )


# ---------------------------------------------------------------------------
# RemoteArtifactStore — write_artifact
# ---------------------------------------------------------------------------


def _mock_client_for_upload(key: str, sha256: str) -> MagicMock:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"key": key, "sha256": sha256}
    mock_client.post.return_value = mock_response
    return mock_client


def test_remote_write_artifact_proxy() -> None:
    expected_key = f"{TENANT_ID}/{LIBRARY_ID}/proxies/07/{ASSET_ID}_beach.jpg"
    expected_sha256 = hashlib.sha256(SAMPLE_BYTES).hexdigest()
    mock_client = _mock_client_for_upload(expected_key, expected_sha256)

    store = RemoteArtifactStore(client=mock_client)
    ref = store.write_artifact(
        "proxy", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
        width=2048, height=1365,
    )

    assert ref.key == expected_key
    assert ref.sha256 == expected_sha256

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0] == f"/v1/assets/{ASSET_ID}/artifacts/proxy"
    files = call_kwargs[1]["files"]
    assert "file" in files
    data = call_kwargs[1]["data"]
    assert data["width"] == "2048"
    assert data["height"] == "1365"


def test_remote_write_artifact_thumbnail() -> None:
    expected_key = f"{TENANT_ID}/{LIBRARY_ID}/thumbnails/07/{ASSET_ID}_beach.jpg"
    expected_sha256 = hashlib.sha256(SAMPLE_BYTES).hexdigest()
    mock_client = _mock_client_for_upload(expected_key, expected_sha256)

    store = RemoteArtifactStore(client=mock_client)
    ref = store.write_artifact(
        "thumbnail", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
    )

    assert ref.key == expected_key
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0] == f"/v1/assets/{ASSET_ID}/artifacts/thumbnail"
    # width/height not in form when not provided
    assert "width" not in call_kwargs[1]["data"]
    assert "height" not in call_kwargs[1]["data"]


def test_remote_write_artifact_scene_rep() -> None:
    rep_frame_ms = 12345
    expected_key = f"{TENANT_ID}/{LIBRARY_ID}/scenes/07/{ASSET_ID}_{rep_frame_ms:010d}.jpg"
    expected_sha256 = hashlib.sha256(SAMPLE_BYTES).hexdigest()
    mock_client = _mock_client_for_upload(expected_key, expected_sha256)

    store = RemoteArtifactStore(client=mock_client)
    ref = store.write_artifact(
        "scene_rep", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
        rep_frame_ms=rep_frame_ms,
    )

    assert ref.key == expected_key
    call_kwargs = mock_client.post.call_args
    assert call_kwargs[0][0] == f"/v1/assets/{ASSET_ID}/artifacts/scene_rep"
    assert call_kwargs[1]["data"]["rep_frame_ms"] == str(rep_frame_ms)


def test_remote_write_artifact_file_bytes_are_sent() -> None:
    """Verify the actual bytes are included in the multipart file field."""
    mock_client = _mock_client_for_upload("some/key.jpg", "abc123")
    store = RemoteArtifactStore(client=mock_client)
    store.write_artifact(
        "proxy", ASSET_ID, SAMPLE_BYTES,
        library_id=LIBRARY_ID, rel_path=REL_PATH,
    )

    files = mock_client.post.call_args[1]["files"]
    _name, file_obj, _content_type = files["file"]
    assert file_obj.read() == SAMPLE_BYTES


# ---------------------------------------------------------------------------
# RemoteArtifactStore — read_artifact
# ---------------------------------------------------------------------------


def test_remote_read_artifact() -> None:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = SAMPLE_BYTES
    mock_client.get.return_value = mock_response

    store = RemoteArtifactStore(client=mock_client)
    result = store.read_artifact(
        "some/key.jpg",
        asset_id=ASSET_ID,
        artifact_type="proxy",
    )

    assert result == SAMPLE_BYTES
    mock_client.get.assert_called_once_with(
        f"/v1/assets/{ASSET_ID}/artifacts/proxy",
        params=None,
    )


def test_remote_read_artifact_uses_asset_id_and_type_not_key() -> None:
    """RemoteArtifactStore uses asset_id + artifact_type for the API call, not the raw key."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"some bytes"
    mock_client.get.return_value = mock_response

    store = RemoteArtifactStore(client=mock_client)
    store.read_artifact(
        "this/key/is/ignored.jpg",
        asset_id="ast_SPECIFIC_ASSET",
        artifact_type="thumbnail",
    )

    mock_client.get.assert_called_once_with(
        "/v1/assets/ast_SPECIFIC_ASSET/artifacts/thumbnail",
        params=None,
    )


def test_remote_read_artifact_scene_rep_includes_rep_frame_ms() -> None:
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = b"scene-bytes"
    mock_client.get.return_value = mock_response

    store = RemoteArtifactStore(client=mock_client)
    result = store.read_artifact(
        "ignored-scene-key.jpg",
        asset_id=ASSET_ID,
        artifact_type="scene_rep",
        rep_frame_ms=12345,
    )

    assert result == b"scene-bytes"
    mock_client.get.assert_called_once_with(
        f"/v1/assets/{ASSET_ID}/artifacts/scene_rep",
        params={"rep_frame_ms": 12345},
    )


def test_remote_read_artifact_scene_rep_requires_rep_frame_ms() -> None:
    store = RemoteArtifactStore(client=MagicMock())
    with pytest.raises(ValueError, match="rep_frame_ms"):
        store.read_artifact(
            "ignored-scene-key.jpg",
            asset_id=ASSET_ID,
            artifact_type="scene_rep",
        )


# ---------------------------------------------------------------------------
# get_artifact_store factory
# ---------------------------------------------------------------------------


def test_factory_local_mode(tmp_path: Path) -> None:
    storage = LocalStorage(data_dir=str(tmp_path))
    store = get_artifact_store("local", storage=storage, tenant_id=TENANT_ID)
    assert isinstance(store, LocalArtifactStore)


def test_factory_remote_mode() -> None:
    mock_client = MagicMock()
    store = get_artifact_store("remote", client=mock_client)
    assert isinstance(store, RemoteArtifactStore)


def test_factory_remote_mode_requires_client() -> None:
    with pytest.raises(ValueError, match="client"):
        get_artifact_store("remote")


def test_factory_local_mode_requires_storage_and_tenant_id() -> None:
    with pytest.raises(ValueError, match="storage and tenant_id"):
        get_artifact_store("local", tenant_id=TENANT_ID)

    with pytest.raises(ValueError, match="storage and tenant_id"):
        storage = LocalStorage(data_dir="/tmp")
        get_artifact_store("local", storage=storage)


def test_factory_default_mode_is_local(tmp_path: Path) -> None:
    """Unrecognised / absent mode falls through to local."""
    storage = LocalStorage(data_dir=str(tmp_path))
    store = get_artifact_store("local", storage=storage, tenant_id=TENANT_ID)
    assert isinstance(store, LocalArtifactStore)
