"""What a job's work is made from (src/server/scheduler/inputs.py, ADR-020
decision 4). Beside the API, each clip's stored preview and analysis proxy
are read from the Store by the keys its queue row carries: nothing is
downloaded and no copy is kept (the caches held 2 GB of previews and up to
50 GB of analysis proxies on Robert's Mac). Elsewhere they come from this
machine's caches, filled through the API. A drive that isn't plugged in is
a wait, never a file missing.

Over a real filesystem Store; the database end to end: test_scheduler_e2e.py.
"""

from __future__ import annotations

import contextlib
import errno
import io
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.processing.proxy.analysis_cache import AnalysisProxyCache
from src.processing.proxy.proxy_cache import ProxyCache
from src.producers.runner import NotTried, Waits
from src.server.scheduler.inputs import Inputs
from src.server.storage.files import FileStore
from src.server.storage.local import LocalStorage
from src.server.storage.store import NotFound
from src.shared import previews_location
from src.shared.waits import SCAN

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _caches_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """This machine's caches, the test's own (ProxyCache makes its folder)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

PREVIEW = "ten_a/lib_1/proxies/07/ast_1_photo.webp"
# lib_2 has a folder of its own, on a drive.
AWAY_PREVIEW = "ten_a/lib_2/proxies/03/ast_2_clip.webp"
ANALYSIS = "ten_a/lib_2/analysis/03/ast_2_clip.mp4"
MP4 = b"\x00\x00\x00\x18ftypmp42 the server's"


def _webp(size: tuple[int, int] = (2048, 1536)) -> bytes:
    """A preview as the API keeps it: WebP, 2048 px on its long edge."""
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 120, 30)).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture
def placed(tmp_path: Path):
    """The API's Store on this disk, with a clip's preview in DATA_DIR and
    another's files on lib_2's drive; and its drive's marker."""
    data = tmp_path / "data"
    data.mkdir()
    drive = tmp_path / "Volumes" / "Clips"
    drive.mkdir(parents=True)
    marker = drive / previews_location.MARKER
    marker.write_text("")
    previews_location.write(data, data, None, libraries={"lib_2": drive})
    store = FileStore(LocalStorage(str(data)))
    store.write(PREVIEW, _webp())
    store.write(AWAY_PREVIEW, _webp())
    store.write(ANALYSIS, MP4)
    return store, drive, marker


def _photo(**keys) -> dict:
    return {"asset_id": "ast_1", "library_id": "lib_1", "rel_path": "photo.jpg", "proxy_key": PREVIEW,
            "analysis_proxy_key": None, **keys}


def _video(**keys) -> dict:
    return {"asset_id": "ast_2", "library_id": "lib_2", "rel_path": "clip.mov", "proxy_key": AWAY_PREVIEW,
            "analysis_proxy_key": ANALYSIS, **keys}


# -- Beside the API: from the Store, by key ---------------------------------------------


def test_a_preview_is_read_from_the_store_at_the_size_producers_see(placed):
    store, *_ = placed
    acct = MagicMock()
    image = Image.open(io.BytesIO(Inputs(acct, store).preview_bytes(_photo())))
    assert (image.format, max(image.size)) == ("JPEG", 1280)
    acct.proxy_cache.assert_not_called()


def test_a_preview_already_small_enough_is_as_it_is(placed):
    store, *_ = placed
    small = _webp((640, 480))
    store.write(PREVIEW, small)
    assert Inputs(MagicMock(), store).preview_bytes(_photo()) == small


def test_an_analysis_proxy_is_the_stored_file_held_for_the_block(placed):
    store, drive, _ = placed
    acct = MagicMock()
    with Inputs(acct, store).analysis_proxy(_video()) as path:
        assert path == drive / ANALYSIS and path.read_bytes() == MP4
    acct.analysis_cache.held.assert_not_called()


def test_a_render_saved_beside_the_api_isn_t_kept_twice(placed, tmp_path):
    store, *_ = placed
    acct = MagicMock()
    rendered = tmp_path / "analysis.mp4"
    rendered.write_bytes(b"rendered")
    Inputs(acct, store).rendered(_video(), rendered)
    acct.analysis_cache.put.assert_not_called()  # the render goes with the job's scratch


# -- Nothing stored for a clip: the caches' ways ---------------------------------------


@pytest.mark.parametrize("key", [None, "ten_a/lib_1/proxies/07/ast_1_gone.webp"])
def test_a_preview_with_no_stored_file_is_the_caches(placed, key):
    store, *_ = placed
    acct = MagicMock()
    acct.proxy_cache.return_value.get.return_value = b"the cache's"
    inputs = Inputs(acct, store)
    assert inputs.preview_bytes(_photo(proxy_key=key)) == b"the cache's"
    acct.proxy_cache.assert_called_once_with("lib_1")
    acct.proxy_cache.return_value.get.assert_called_once_with("ast_1", content_hash=None)
    assert inputs.stored_preview(_photo(proxy_key=key)) is None


@pytest.mark.parametrize("key", [None, "ten_a/lib_2/analysis/03/ast_2_gone.mp4"])
def test_an_analysis_proxy_with_no_stored_file_is_the_caches(placed, tmp_path, key):
    store, *_ = placed
    acct = MagicMock()
    acct.analysis_cache.held.return_value = contextlib.nullcontext(tmp_path / "cached.mp4")
    with Inputs(acct, store).analysis_proxy(_video(analysis_proxy_key=key)) as path:
        assert path == tmp_path / "cached.mp4"
    acct.analysis_cache.held.assert_called_once_with("ast_2")


def test_what_the_work_raises_in_the_block_is_the_work_s(placed):
    """A NotFound from inside the block isn't taken for the file missing."""
    store, *_ = placed
    acct = MagicMock()
    with pytest.raises(NotFound, match="the work's"), Inputs(acct, store).analysis_proxy(_video()):
        raise NotFound("the work's")
    acct.analysis_cache.held.assert_not_called()


# -- A drive that isn't plugged in ------------------------------------------------------


def test_files_on_a_drive_not_plugged_in_wait_and_aren_t_taken_for_missing(placed):
    store, _, marker = placed
    acct = MagicMock()
    marker.unlink()
    inputs = Inputs(acct, store)
    with pytest.raises(Waits, match="can't be reached"):
        inputs.preview_bytes(_video())
    with pytest.raises(Waits, match="can't be reached"), inputs.analysis_proxy(_video()):
        pass
    acct.proxy_cache.assert_not_called()
    acct.analysis_cache.held.assert_not_called()
    assert inputs.preview_bytes(_photo()) is not None, "what can be reached still is"


# -- Elsewhere: this machine's caches, filled through the API ---------------------------


class _Client:
    """The API's downloads: what was asked for."""

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []

    @contextmanager
    def stream(self, path: str, **kwargs: object):
        self.calls.append(path)
        asset_id = path.split("/")[3]
        status, body = (200, self.bodies[asset_id]) if asset_id in self.bodies else (404, b"")
        yield MagicMock(status_code=status, iter_bytes=lambda chunk_size=0: iter([body]))


class _Elsewhere:
    """An account on a machine the API's Store isn't on: real caches."""

    def __init__(self, tmp_path: Path, bodies: dict[str, bytes]) -> None:
        self.client = _Client(bodies)
        self.analysis_cache = AnalysisProxyCache(self.client, cache_dir=tmp_path / "analysis", max_bytes=10**9)
        self.previews = ProxyCache(max_edge=1280)

    def proxy_cache(self, library_id: str) -> ProxyCache:
        return self.previews


def test_elsewhere_an_analysis_proxy_is_downloaded_into_the_cache_once(tmp_path):
    acct = _Elsewhere(tmp_path, {"ast_2": b"downloaded"})
    inputs = Inputs(acct, None)
    for _ in range(2):
        with inputs.analysis_proxy(_video()) as path:
            assert path.parent == tmp_path / "analysis" and path.read_bytes() == b"downloaded"
    assert acct.client.calls == ["/v1/assets/ast_2/artifacts/lumiverb.video.analysis_proxy"]  # by its name


def test_elsewhere_a_render_is_kept_in_the_cache(tmp_path):
    acct = _Elsewhere(tmp_path, {})
    rendered = tmp_path / "analysis.mp4"
    rendered.write_bytes(b"rendered")
    Inputs(acct, None).rendered(_video(), rendered)
    assert acct.analysis_cache.path_for("ast_2").read_bytes() == b"rendered"


def test_elsewhere_a_preview_is_the_cache_s(tmp_path):
    acct = _Elsewhere(tmp_path, {})
    acct.previews.put_scan("ast_1", b"cached", "")
    inputs = Inputs(acct, None)
    assert inputs.preview_bytes(_photo()) == b"cached"
    assert inputs.stored_preview(_photo()) is None


# -- Originals, for a producer that reads the storage (src/processing/originals.py) --------


class _Library:
    """The account as a clip's original sees it: its library's originals at
    the last look, and whether its storage is gone when a job looks again."""

    def __init__(self, originals, gone: bool = False) -> None:
        self._originals, self.gone, self.away = originals, gone, []

    def originals(self, library_id: str):
        return self._originals

    def storage_gone(self, library_id: str) -> bool:
        return self.gone

    def unreachable(self, library_id: str) -> None:
        self.away.append(library_id)


class _Unreadable:
    """Originals whose files can't be read: no permission, say."""

    def local_file(self, rel_path: str):
        raise PermissionError(errno.EACCES, "Permission denied", rel_path)


def _clip(rel_path: str = "a.jpg") -> dict:
    return {"asset_id": "ast_1", "library_id": "lib_1", "rel_path": rel_path}


def test_an_original_is_its_file_for_the_block(tmp_path):
    from src.processing.originals import Folder

    (tmp_path / "a.jpg").write_bytes(b"original")
    with Inputs(_Library(Folder(tmp_path, "lib_1")), None).original(_clip()) as source:
        assert source.read_bytes() == b"original"


def test_an_original_missing_from_storage_that_s_there_waits_for_its_scan(tmp_path):
    from src.processing.originals import Folder

    acct = _Library(Folder(tmp_path, "lib_1"))
    with pytest.raises(Waits) as waits, Inputs(acct, None).original(_clip("gone.jpg")):
        pass
    assert waits.value.reason == SCAN and acct.away == []


def test_an_original_that_can_t_be_read_is_the_clip_s_saying_why():
    """Never taken for gone: before, it waited for a scan, which can't read it either."""
    with pytest.raises(PermissionError, match="Permission denied"), Inputs(_Library(_Unreadable()), None).original(_clip()):
        pass


def test_one_that_can_t_be_read_because_the_storage_went_is_not_tried():
    acct = _Library(_Unreadable(), gone=True)
    with pytest.raises(NotTried), Inputs(acct, None).original(_clip()):
        pass
    assert acct.away == ["lib_1"]
    with Inputs(acct, None).originals([_clip()]) as found:
        assert found == {}, "quietly left out: each clip's own original() says why"


# -- Where the account's Store comes from ------------------------------------------------


def test_an_account_reads_the_store_only_where_it_s_on_this_disk(monkeypatch):
    from src.server.scheduler import service

    monkeypatch.setattr(service, "stores_here", lambda: True)
    assert service.here_store() is not None
    monkeypatch.setattr(service, "stores_here", lambda: False)
    assert service.here_store() is None
