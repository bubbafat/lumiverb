"""Storage keys never reach outside DATA_DIR, and nothing sets one over the API."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.storage.local import LocalStorage, UnsafeKeyError

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("key", [
    "/etc/passwd",
    "../outside.jpg",
    "ten_1/../../outside.jpg",
    "ten_1/lib/../../../etc/passwd",
    "",
    "a\x00b",
    "\\\\server\\share",
])
def test_abs_path_refuses_keys_that_leave_data_dir(tmp_path: Path, key: str) -> None:
    storage = LocalStorage(str(tmp_path / "data"))
    with pytest.raises(UnsafeKeyError):
        storage.abs_path(key)


def test_read_write_and_exists_go_through_the_check(tmp_path: Path) -> None:
    data = tmp_path / "data"
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    storage = LocalStorage(str(data))
    with pytest.raises(UnsafeKeyError):
        storage.write("../secret.txt", b"overwritten")
    with pytest.raises(UnsafeKeyError):
        storage.exists("../secret.txt")
    assert secret.read_text() == "x"


def test_abs_path_keeps_ordinary_keys(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    key = "ten_1/lib_1/thumbnails/07/ast_01_photo..webp"  # ".." inside a name is fine
    assert storage.abs_path(key) == tmp_path / key
    storage.write("ten_1/lib_1/proxies/00/a.webp", b"ok")
    assert storage.exists("ten_1/lib_1/proxies/00/a.webp")


def test_local_artifact_store_read_refuses_outside_keys(tmp_path: Path) -> None:
    from src.server.storage.artifact_store import LocalArtifactStore

    (tmp_path / "secret").write_bytes(b"s")
    store = LocalArtifactStore(LocalStorage(str(tmp_path / "data")), "ten_1")
    with pytest.raises(UnsafeKeyError):
        store.read_artifact("../secret", asset_id="ast_x", artifact_type="proxy")


def test_routes_that_took_paths_from_the_client_are_gone() -> None:
    from fastapi.routing import APIRoute

    from src.server.api.main import app

    paths = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert ("/v1/assets/{asset_id}/thumbnail-key", "POST") not in paths
    assert ("/v1/video/reset", "POST") not in paths


def test_scene_results_carry_no_storage_keys() -> None:
    from src.server.api.routers.video import SceneResult

    scene = SceneResult(scene_index=0, start_ms=0, end_ms=1, rep_frame_ms=0,
                        proxy_key="/etc/passwd", thumbnail_key="../x")  # type: ignore[call-arg]
    assert "proxy_key" not in scene.model_dump() and "thumbnail_key" not in scene.model_dump()
