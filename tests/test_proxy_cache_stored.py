"""The scheduler reads the previews the API stores on this machine where they
are (src/processing/proxy/proxy_cache.py `stored`, src/server/scheduler/service.py
stored_preview): CLIP, faces, OCR and descriptions see a clip's stored preview
at the cache's size, and the cache keeps no second copy (2 GB of them on
Robert's Mac). A clip whose stored preview isn't here (none yet, a drive
unplugged, another machine's server) is read from the cache, its original or
the server, as before; and the copies an earlier scheduler kept go at its
start (forget_preview_copies)."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from src.processing.proxy.proxy_cache import ProxyCache

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _preview(path: Path, size: tuple[int, int] = (2048, 1536)) -> Path:
    """A stored preview as the API keeps it: WebP, 2048 px on its long edge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (90, 120, 30)).save(path, format="WEBP")
    return path


def test_a_stored_preview_is_read_where_it_is_at_the_caches_size_with_no_copy(tmp_path):
    stored = _preview(tmp_path / "previews" / "ast_1.webp")
    client = MagicMock()
    cache = ProxyCache(max_edge=1280, client=client, stored=lambda a: stored if a == "ast_1" else None)
    image = Image.open(io.BytesIO(cache.get("ast_1", "a.jpg")))
    assert (image.format, max(image.size)) == ("JPEG", 1280)
    assert not cache.has("ast_1") and not cache.sha_path("ast_1").exists()
    assert not client._client.get.called, "nothing downloaded"


def test_a_copy_cached_before_goes_once_the_stored_one_is_read(tmp_path):
    stored = _preview(tmp_path / "ast_1.webp")
    cache = ProxyCache(max_edge=1280, stored=lambda a: stored)
    cache.put_scan("ast_1", b"a copy from before", "sha")
    assert cache.get("ast_1") != b"a copy from before"
    assert not cache.has("ast_1") and not cache.sha_path("ast_1").exists(), "the duplicate goes"


def test_one_not_stored_here_is_read_as_before(tmp_path):
    cache = ProxyCache(max_edge=1280, stored=lambda a: None)
    cache.put_scan("ast_2", b"cached", "")
    assert cache.get("ast_2") == b"cached"
    assert cache.stored_path("ast_2") is None


def test_a_lookup_that_fails_is_the_cache_not_an_error(tmp_path):
    def broken(asset_id: str):
        raise ConnectionError("the database isn't answering")

    cache = ProxyCache(max_edge=1280, stored=broken)
    cache.put_scan("ast_3", b"cached", "")
    assert cache.stored_path("ast_3") is None
    assert cache.get("ast_3") == b"cached"


def _service(monkeypatch, data_dir: Path, *, here: bool = True):
    from src.server.scheduler import service

    monkeypatch.setattr(service, "stores_here", lambda: here)
    monkeypatch.setattr("src.server.config.get_settings", lambda: SimpleNamespace(data_dir=str(data_dir)))
    return service


def _copies(folder: Path, n: int) -> None:
    folder.mkdir(parents=True)
    for i in range(n):
        (folder / f"ast_{i}").write_bytes(b"x" * 1000)
        (folder / f"ast_{i}.sha").write_text("sha")


def test_the_copies_an_earlier_scheduler_kept_go_at_its_start(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_CACHE_HOME", str(data / "cache"))  # where run puts the scheduler's
    _copies(data / "cache" / "lumiverb" / "proxies", 3)
    service = _service(monkeypatch, data)
    assert service.forget_preview_copies() == (6, 3009)
    assert not (data / "cache" / "lumiverb" / "proxies").exists()
    assert service.forget_preview_copies() is None  # nothing left to do


def test_a_cache_outside_the_servers_data_or_with_previews_elsewhere_is_left_alone(tmp_path, monkeypatch):
    data = tmp_path / "data"
    shared = tmp_path / "home-cache"  # the CLI's and the Mac app's: never the scheduler's to clear
    monkeypatch.setenv("XDG_CACHE_HOME", str(shared))
    _copies(shared / "lumiverb" / "proxies", 1)
    assert _service(monkeypatch, data).forget_preview_copies() is None
    assert (shared / "lumiverb" / "proxies" / "ast_0").exists()
    monkeypatch.setenv("XDG_CACHE_HOME", str(data / "cache"))
    _copies(data / "cache" / "lumiverb" / "proxies", 1)
    assert _service(monkeypatch, data, here=False).forget_preview_copies() is None  # previews kept elsewhere
    assert (data / "cache" / "lumiverb" / "proxies" / "ast_0").exists()
