"""The Store's contract (src/server/storage/store.py, ADR-020 decision 2):
what every provider does, run against each one there is. A provider is
added to PROVIDERS with a way to make some of its keys unreachable (a
drive unplugged, a bucket down); everything else is the protocol.

Keys and their builders are pinned here too: every row that names a key
holds one, so they never change.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.server.storage import store as store_module
from src.server.storage.store import NotFound, Store, Unavailable

pytestmark = pytest.mark.fast

KEY = "ten_a/lib_1/proxies/07/ast_1_photo.webp"
OTHER = "ten_a/lib_1/thumbnails/07/ast_1_photo.webp"
ELSEWHERE = "ten_a/lib_2/analysis/03/ast_2_clip.mp4"
# Kept where the provider can make it unreachable.
AWAY = "ten_a/lib_9/scenes/00/ast_9_0000001000.jpg"


@dataclass
class Provided:
    store: Store
    unplug: Callable[[], None]  # AWAY's storage can't be reached
    plug_in: Callable[[], None]  # and can again
    # Whether what's kept elsewhere is still reached meanwhile (a cache is one folder: all or nothing).
    in_parts: bool = True


def _files(tmp_path: Path) -> Provided:
    from src.server.storage.files import FileStore
    from src.server.storage.local import LocalStorage
    from src.shared import previews_location

    data = tmp_path / "data"
    data.mkdir()
    drive = tmp_path / "Volumes" / "Clips"  # lib_9's folder of its own, on a drive
    drive.mkdir(parents=True)
    marker = drive / previews_location.MARKER
    marker.write_text("")
    previews_location.write(data, data, None, libraries={"lib_9": drive})
    return Provided(FileStore(LocalStorage(str(data))), unplug=marker.unlink,
                    plug_in=lambda: marker.write_text(""))


def _cache(tmp_path: Path) -> Provided:
    """A Cache is a Store with a cap (ADR-020 decision 3): held to the same contract, under its cap."""
    from src.server.storage.cache import FileCache

    folder = tmp_path / "cache"
    away = {"now": False}

    def root() -> Path:
        if away["now"]:
            raise FileNotFoundError("its drive isn't plugged in")
        return folder

    return Provided(FileCache(root, cap_bytes=10**9), unplug=lambda: away.update(now=True),
                    plug_in=lambda: away.update(now=False), in_parts=False)


PROVIDERS = {"files": _files, "cache": _cache}


@pytest.fixture(params=sorted(PROVIDERS))
def provided(request, tmp_path) -> Iterator[Provided]:
    yield PROVIDERS[request.param](tmp_path)


@pytest.fixture
def store(provided) -> Store:
    return provided.store


def test_each_provider_is_a_store(store):
    assert isinstance(store, Store)


# -- Kept and read back ------------------------------------------------------------


def test_what_is_written_is_read_back_whole(store):
    import time

    store.write(KEY, b"a proxy")
    assert store.read(KEY) == b"a proxy"
    assert store.size(KEY) == 7 and store.exists(KEY)
    st = store.stat(KEY)
    assert st.size == 7 and abs(st.modified - time.time()) < 60
    with store.open(KEY) as f:
        f.seek(2)  # a range is a seek
        assert f.read(3) == b"pro"


def test_a_file_object_is_written_as_well_as_bytes(store):
    store.write(KEY, io.BytesIO(b"x" * 300_000))
    assert store.size(KEY) == 300_000


def test_written_again_it_is_the_new_one_only(store):
    store.write(KEY, b"a much longer first version")
    store.write(KEY, b"second")
    assert store.read(KEY) == b"second"


def test_a_key_with_no_file_is_not_found(store):
    assert not store.exists(KEY)
    for read in (store.read, store.size, store.stat, store.open, lambda k: store.local_file(k).__enter__()):
        with pytest.raises(NotFound):
            read(KEY)


def test_not_found_is_a_file_not_found_and_unavailable_never_is():
    assert issubclass(NotFound, FileNotFoundError)
    assert issubclass(Unavailable, OSError) and not issubclass(Unavailable, FileNotFoundError)


def test_deleted_it_is_gone_and_deleting_again_is_fine(store):
    store.write(KEY, b"x")
    store.delete(KEY)
    store.delete(KEY)
    assert not store.exists(KEY)
    with pytest.raises(NotFound):
        store.read(KEY)


# -- Listed --------------------------------------------------------------------------


def test_listed_by_prefix_each_once(store):
    for key in (KEY, OTHER, ELSEWHERE):
        store.write(key, b"x")
    assert sorted(store.list("ten_a/lib_1/")) == sorted([KEY, OTHER])
    assert sorted(store.list("ten_a/lib_1/proxies/0")) == [KEY]
    assert sorted(store.list("ten_a/")) == sorted([KEY, OTHER, ELSEWHERE])
    assert list(store.list("ten_b/")) == []


# -- Handed to a tool ----------------------------------------------------------------


def test_a_tool_reads_it_from_a_path(store):
    store.write(KEY, b"frames")
    with store.local_file(KEY) as path:
        assert Path(path).read_bytes() == b"frames"


def test_a_tool_writes_it_to_a_path_kept_when_the_block_ends(store):
    with store.writing(ELSEWHERE, suffix=".mp4") as path:
        assert path.suffix == ".mp4" and not path.exists(), "nothing there: a tool doesn't ask to write over it"
        path.write_bytes(b"a render")
        assert not store.exists(ELSEWHERE) and list(store.list("ten_a/")) == [], "never seen half written"
    assert store.read(ELSEWHERE) == b"a render"
    assert list(store.list("ten_a/")) == [ELSEWHERE]


def test_a_tool_that_fails_leaves_nothing_and_the_old_one_as_it_was(store):
    store.write(ELSEWHERE, b"the old render")
    with pytest.raises(RuntimeError), store.writing(ELSEWHERE, suffix=".mp4") as path:
        path.write_bytes(b"half a ren")
        raise RuntimeError("ffmpeg died")
    assert store.read(ELSEWHERE) == b"the old render"
    assert list(store.list("ten_a/")) == [ELSEWHERE]


# -- Unreachable -------------------------------------------------------------------------


def test_unreachable_is_unavailable_never_not_found(provided):
    store = provided.store
    store.write(AWAY, b"a frame")
    store.write(KEY, b"a proxy")
    provided.unplug()
    for touch in (store.read, store.size, store.stat, store.exists, store.open, store.delete,
                  lambda k: store.write(k, b"x"), lambda k: store.local_file(k).__enter__(),
                  lambda k: store.writing(k).__enter__(), lambda k: list(store.list("ten_a/lib_9/"))):
        with pytest.raises(Unavailable):
            touch(AWAY)
    with pytest.raises(Unavailable):
        list(store.list("ten_a/"))  # a list that isn't whole never is
    if provided.in_parts:
        assert store.read(KEY) == b"a proxy", "what can be reached still is"
    provided.plug_in()
    assert store.read(AWAY) == b"a frame", "nothing was taken for missing"


# -- Keys ------------------------------------------------------------------------------


def test_the_keys_are_what_they_always_were():
    asset, face = "ast_01HZ0000000000000000000003", "face_01HZ0000000000000000000003"
    assert store_module.proxy_key("ten_a", "lib_1", asset, "a/b/photo.jpg") == (
        "ten_a/lib_1/proxies/79/ast_01HZ0000000000000000000003_photo.webp")
    assert store_module.thumbnail_key("ten_a", "lib_1", asset, "photo.jpg") == (
        "ten_a/lib_1/thumbnails/79/ast_01HZ0000000000000000000003_photo.webp")
    assert store_module.video_preview_key("ten_a", "lib_1", asset, "trip/clip.MOV") == (
        "ten_a/lib_1/previews/79/ast_01HZ0000000000000000000003_clip.mp4")
    assert store_module.analysis_proxy_key("ten_a", "lib_1", asset, "trip/clip.MOV") == (
        "ten_a/lib_1/analysis/79/ast_01HZ0000000000000000000003_clip.mp4")
    assert store_module.scene_rep_key("ten_a", "lib_1", asset, 1500) == (
        "ten_a/lib_1/scenes/79/ast_01HZ0000000000000000000003_0000001500.jpg")
    assert store_module.face_crop_key("ten_a", "lib_1", face) == (
        "ten_a/lib_1/face_crops/79/face_01HZ0000000000000000000003.webp")
    assert store_module.face_crop_key_of("ten_a", "lib_1", "face_1") is None


def test_an_install_names_a_provider_there_is(monkeypatch):
    from pydantic import ValidationError

    from src.server.config import Settings

    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("TENANT_DATABASE_URL_TEMPLATE", "postgresql://x/{tenant_id}")
    monkeypatch.setenv("STORAGE_PROVIDER", "s3")
    with pytest.raises(ValidationError, match="STORAGE_PROVIDER='s3' isn't a storage provider"):
        Settings()
    monkeypatch.setenv("STORAGE_PROVIDER", "local")
    assert Settings().storage_provider == "local"
