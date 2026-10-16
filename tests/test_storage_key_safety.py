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


@pytest.mark.parametrize("key", ["../secret", "ten_1/../../secret", "/etc/passwd", "ten_1/\x00", ""])
def test_the_store_refuses_outside_keys_for_everything(tmp_path: Path, key: str) -> None:
    from src.server.storage.files import FileStore

    (tmp_path / "secret").write_bytes(b"s")
    store = FileStore(LocalStorage(str(tmp_path / "data")))
    for touch in (store.open, store.read, store.size, store.exists, store.delete,
                  lambda k: store.write(k, b"x"), lambda k: store.local_file(k).__enter__(),
                  lambda k: store.writing(k).__enter__()):
        with pytest.raises(UnsafeKeyError):
            touch(key)
    assert (tmp_path / "secret").read_bytes() == b"s"


def test_routes_that_took_paths_from_the_client_are_gone() -> None:
    from src.server.api.main import app
    from tests.app_routes import api_routes

    paths = {(path, m) for path, methods in api_routes(app) for m in methods}
    assert ("/v1/assets/{asset_id}/artifacts/{artifact_type}", "POST") in paths  # the walk found the routes
    assert ("/v1/assets/{asset_id}/thumbnail-key", "POST") not in paths
    assert ("/v1/video/reset", "POST") not in paths


def test_scene_results_carry_no_storage_keys() -> None:
    from src.server.api.routers.video import SceneResult

    scene = SceneResult(scene_index=0, start_ms=0, end_ms=1, rep_frame_ms=0,
                        proxy_key="/etc/passwd", thumbnail_key="../x")  # type: ignore[call-arg]
    assert "proxy_key" not in scene.model_dump() and "thumbnail_key" not in scene.model_dump()
